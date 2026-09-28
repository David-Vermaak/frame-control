"""The connection to the active headset: which address to use, and every step of getting there.

A background thread (Link) keeps one SSH connection to the active headset open
and publishes what it's doing, stage by stage, for the page's connection pill:

  1. network   checking this computer's network (gateway, Wi-Fi, Tailscale)
  2. find      finding the headset: every address probed on port 22 at once
  3. ssh       opening SSH to the address that answered
  4. identity  checking the headset's identity (its pinned host key)
  5. login     logging in as the device's user
  then connected (network, address, round trip), or failed at a stage with a
  plain reason and a countdown to the next try.

Addresses go in the order frame_devices.order_addresses gives. All are probed
at once; the best-ranked one that answers wins, waiting a moment (PREFER) for a
better-ranked address that's still trying, happy-eyeballs style. If SSH to the
winner fails in a way another address could fix (a different device answered,
the link dropped), the next one that answered is tried.

The server hands in `apply(alias, host_opts)`, which points every ssh, scp and
rsync it runs at the alias with `-o HostName=<address>` and friends, so they all
follow. Where ssh can share one connection (not Windows), the master connection
lives here; it reconnects when it dies, when this computer changes networks, and
when the page asks.

Python stdlib only. Runs on this computer, never on the Frame.
"""
import copy
import ipaddress
import queue
import re
import socket
import subprocess
import threading
import time

import frame_devices
import frame_host
import frame_network

PROBE_TIMEOUT = 4      # seconds for a TCP answer on port 22
PREFER = 0.35          # how long an answer waits for a better-ranked address still trying
HANDSHAKE_TIMEOUT = 25
TICK = 2               # the loop's heartbeat
NETWORK_EVERY = 5      # how often the network fingerprint is read
TAILSCALE_EVERY = 30
RETRY = (5, 10, 20, 30)  # seconds before automatic retries after a failure
REQUEST_GAP = 5        # a request may start a new attempt this long after the last one

STAGES = [("network", "Checking this computer's network"), ("find", "Finding the headset"),
          ("ssh", "Opening SSH"), ("identity", "Checking the headset's identity"),
          ("login", "Logging in")]

# What ssh -v prints at each step (OpenSSH on macOS, Linux and Windows).
CONNECTING = re.compile(r"Connecting to (\S+) \[([^\]]+)\] port (\d+)")
ESTABLISHED = re.compile(r"Connection established")
HOSTKEY = re.compile(r"Server host key: (\S+) (\S+)")
KNOWN = re.compile(r"is known and matches")
ADDED = re.compile(r"Permanently added")
CHANGED = re.compile(r"REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed")
UNKNOWN = re.compile(r"No \S+ host key is known for")
AUTH_START = re.compile(r"Authentications that can continue|Next authentication method")
AUTHED = re.compile(r"Authenticated to |Authentication succeeded")
DENIED = re.compile(r"Permission denied")


def now():
    return time.time()


def ssh_g(alias):
    """(hostname, port, user, proxied) from `ssh -G ALIAS`, for a headset that's only an
    ssh alias. proxied: it goes through ProxyJump or ProxyCommand, so only ssh can reach it."""
    try:
        out = subprocess.run(["ssh", "-G", alias], capture_output=True, stdin=subprocess.DEVNULL, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    got = {}
    for line in out.splitlines():
        k, _, v = line.partition(" ")
        if k in ("hostname", "port", "user", "proxyjump", "proxycommand") and k not in got:
            got[k] = v.strip()
    port = int(got["port"]) if got.get("port", "").isdigit() else 22
    proxied = any(got.get(k) not in (None, "", "none") for k in ("proxyjump", "proxycommand"))
    return got.get("hostname") or alias, port, got.get("user"), proxied


def probe(host, port, timeout=PROBE_TIMEOUT, update=None):
    """Try a TCP connection to host:port. -> {"state", "detail", "ip", "rtt_ms"}.

    state: answered, unresolved, timeout, refused, unreachable or error. update(fields)
    reports progress (resolving, trying) as it happens."""
    update = update or (lambda **_: None)
    update(state="resolving", detail="Looking up the name")
    deadline = now() + timeout
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as e:
        return {"state": "unresolved", "detail": "Can't find this name on the network", "error": str(e)}
    last = None
    infos = infos[:4]
    for n, (family, kind, proto, _, addr) in enumerate(infos):
        ip = addr[0]
        if family == socket.AF_INET6 and len(addr) > 3 and addr[3] and "%" not in ip:
            try:  # a link-local IPv6 address only works with its interface
                ip = f"{ip}%{socket.if_indextoname(addr[3])}"
            except (OSError, AttributeError):
                pass
        left = deadline - now()
        if left <= 0:
            break
        update(state="trying", detail=f"Trying {ip}", ip=ip)
        s = socket.socket(family, kind, proto)
        # Share the time out, so one address that never answers (a dead IPv6 route,
        # say) leaves the others their turn.
        s.settimeout(left / (len(infos) - n))
        t0 = time.monotonic()
        try:
            s.connect(addr)
            rtt = round((time.monotonic() - t0) * 1000, 1)
            return {"state": "answered", "detail": f"Answered in {rtt:g} ms", "ip": ip, "rtt_ms": rtt}
        except socket.timeout:
            last = {"state": "timeout", "detail": "No answer", "ip": ip}
        except ConnectionRefusedError:
            last = {"state": "refused", "detail": "Refused: SSH isn't on at this address", "ip": ip}
        except OSError as e:
            last = {"state": "unreachable", "detail": f"Can't get there ({e.strerror or e})", "ip": ip}
        finally:
            s.close()
    return last or {"state": "timeout", "detail": "No answer"}


def ssh_target(host, ip):
    """Where ssh should go for an address whose probe answered from `ip`: that IP, so ssh
    doesn't look the name up again and try an address that didn't answer."""
    try:
        ipaddress.ip_address((ip or "").split("%")[0])
        return ip
    except ValueError:
        return host


def probe_raw(host, port, result):
    """ssh's own wording for a failed probe, so the server's UNREACHABLE table explains it."""
    return {"unresolved": f"ssh: Could not resolve hostname {host}: not found",
            "refused": f"ssh: connect to host {host} port {port}: Connection refused",
            "unreachable": f"ssh: connect to host {host} port {port}: No route to host",
            }.get(result["state"], f"ssh: connect to host {host} port {port}: Operation timed out")


class Link:
    def __init__(self, registry, *, env_alias, mux_base, control, apply, explain):
        self.reg = registry
        self.override = env_alias       # FRAME_ALIAS, if set: the headset this server starts on
        self.work_lock = threading.Lock()  # the server's: held, no install starts (see server.working)
        self.work = lambda: 0              # how many installs are running
        self.deferred = False              # a login change from ~/.ssh/config waiting for them
        self.session_alias = env_alias  # ...and stays selectable after switching away
        self.mux_base = list(mux_base)  # ["ssh", "-o", "BatchMode=yes", ControlPath...]
        self.control = control          # ControlPath, or None where ssh can't share connections
        self.apply = apply              # apply(alias, host_opts): point every ssh command at the headset
        self.explain = explain          # ssh error text -> plain reason, or None
        self.cond = threading.Condition()
        self.version = 0
        self.stopped = False
        self.kicks = []                 # reasons someone asked for a (re)connect
        self.busy = False               # the loop is handling kicks
        self.state = {"phase": "idle", "reason": None, "device": None, "network": None, "stages": [],
                      "probes": [], "via": None, "error": None, "retry_at": None, "attempt": 0,
                      "started": None, "finished": None, "tests": {}, "devices_rev": 0}
        self.master = None              # the ssh ControlMaster process, if we started it
        self.opts = []                  # host options of the current connection
        self.alias = None
        self.fails = 0
        self.last_fp = None
        self.last_attempt = 0
        self.config_mtime = None
        self.thread = None
        self.attempt_gen = 0
        self.pending = None             # an ssh handshake still running
        self.gen = 0                    # bumped when the headset or its login changes: older attempts are void
        self.route_lock = threading.Lock()
        self.routed = None              # the device id every ssh command points at

    # ---- publishing ----
    def publish(self, **fields):
        with self.cond:
            self.state.update(fields)
            self.version += 1
            self.cond.notify_all()

    def snapshot(self):
        with self.cond:
            snap = copy.deepcopy(self.state)
            snap["version"] = self.version
        snap["now"] = now()
        return snap

    def wait(self, version, timeout):
        """The state once its version passes `version`, or None after `timeout` seconds."""
        with self.cond:
            if not self.cond.wait_for(lambda: self.version > version or self.stopped, timeout):
                return None
        return self.snapshot()

    def stage(self, sid, state, detail=None):
        """Move one stage along (pending -> active -> done or failed) and publish."""
        with self.cond:
            for s in self.state["stages"]:
                if s["id"] == sid:
                    if state == "active" and s["state"] != "active":
                        s["started"] = now()
                    if state in ("done", "failed", "skipped"):
                        s["ended"] = now()
                        s["started"] = s["started"] or s["ended"]
                    s["state"] = state
                    if detail is not None:
                        s["detail"] = detail
            self.version += 1
            self.cond.notify_all()

    def probe_update(self, index, attempt=None, **fields):
        with self.cond:
            if attempt is not None and attempt != self.state["attempt"]:
                return  # a probe from an earlier attempt, still finishing: not this one's row
            if index < len(self.state["probes"]):
                self.state["probes"][index].update(fields)
            self.version += 1
            self.cond.notify_all()

    def devices_changed(self):
        with self.cond:
            self.state["devices_rev"] += 1
            self.version += 1
            self.cond.notify_all()

    # ---- control from the server ----
    def start(self):
        self.thread = threading.Thread(target=self.run, name="frame-link", daemon=True)
        self.thread.start()

    def kick(self, reason):
        with self.cond:
            self.kicks.append(reason)
            self.cond.notify_all()

    def stop(self):
        with self.cond:
            self.stopped = True
            self.cond.notify_all()
        self.close_master()
        if self.thread:
            self.thread.join(5)  # an attempt in progress notices `stopped` and ends
        self.close_master()

    def alive(self):
        if self.state["phase"] != "connected" or self.kicks:
            return False
        if not self.control:
            return True
        return self.master is None or self.master.poll() is None

    def ensure(self, wait=20):
        """Called before a command: make sure a connection is up, or being tried.

        Waits (up to `wait` s) for an attempt already running, or starts one if the
        last ended a while ago. Never raises: if the headset can't be reached, the
        command runs anyway and fails with ssh's own error, as it always has."""
        with self.cond:
            if self.stopped or self.alive():
                return
            if self.state["phase"] != "connecting" and not self.kicks and now() - self.last_attempt > REQUEST_GAP:
                self.kicks.append("request")
            self.cond.wait_for(lambda: self.stopped or (not self.kicks and not self.busy and
                                                        self.state["phase"] != "connecting"), wait)

    def use(self, device_id):
        """Switch to another headset: one from the registry, or back to FRAME_ALIAS."""
        if self.session_alias and device_id == self.bare(self.session_alias)["id"] \
                and not self.reg.by_alias(self.session_alias):
            self.override = self.session_alias
        else:
            self.reg.set_active(device_id)
            self.override = None
        self.invalidate()

    def invalidate(self):
        """The headset in use, or how to log in to it, changed: from now on commands go to
        the one now selected (never the last one), any attempt still running is void,
        and ensure() waits for the connector to reach it."""
        device = self.active_device()
        with self.route_lock:
            self.gen += 1
            self.apply(device["alias"], self.first_route(device))
            self.routed = None  # the next attempt routes again
        with self.cond:
            # Say so at once: the page clears the old headset's panels when the device changes.
            self.state.update(phase="connecting", device=self.public_device(device), via=None, error=None,
                              retry_at=None, probes=[], stages=[])
            self.kicks.append("switch")
            self.version += 1
            self.cond.notify_all()
        self.devices_changed()

    def first_route(self, device):
        """Where commands go before any address has answered: the first one, with the
        headset's own pinned identity, so nothing reaches another device meanwhile."""
        return self.host_opts(device, device["addresses"][0]["host"] if device["addresses"] else None)

    @staticmethod
    def route_key(device):
        return device["id"], device.get("user"), device.get("port")

    def lost(self, message):
        """A command couldn't reach the headset (Windows has no master to watch)."""
        if self.state["phase"] == "connected":
            self.kick(f"lost: {message}")

    # ---- the device this server talks to ----
    def active_device(self):
        """The active headset from the registry, or a stand-in for a bare ssh alias."""
        if self.override:
            return self.reg.by_alias(self.override) or self.bare(self.override)
        want = self.reg.active()
        if want:
            try:
                return self.reg.get(want)
            except frame_devices.DeviceError:
                pass
        devices = self.reg.devices()
        if devices:
            return devices[0]
        if self.reg.emptied():
            return self.NONE  # every headset was removed: reach nothing until one is added
        return self.bare("frame")  # never set up here, or set up before the registry existed

    NONE = {"id": "none", "name": "No headset", "alias": "frame-control-no-headset", "user": None, "port": None,
            "addresses": [], "transient": True, "none": True, "identity_files": []}

    @staticmethod
    def bare(alias):
        """A headset that's only an ssh alias (no Set Up Connection block): ssh's config decides."""
        return {"id": f"alias-{alias}", "name": alias, "alias": alias, "user": None, "port": None,
                "addresses": [], "transient": True, "identity_files": []}

    def host_opts(self, device, host):
        """What every ssh command adds to reach DEVICE at HOST."""
        if device.get("none"):
            return ["-o", "HostName=no-headset.invalid"]  # fails at once, with ssh's own "can't resolve"
        if device.get("transient"):
            return []
        if not host:  # a headset with no addresses: reach nothing, not whatever ~/.ssh/config says
            return ["-o", "HostName=no-address.invalid"]
        return ["-o", f"HostName={frame_devices.ssh_host(host)}",
                "-o", f"HostKeyAlias={frame_devices.host_key_alias(device['id'])}",
                "-o", f"UserKnownHostsFile={frame_devices.known_hosts_opt(device['id'])}", "-o", "HashKnownHosts=no",
                "-o", f"User={device['user']}", "-o", f"Port={device['port']}"]

    def public_device(self, d):
        return {k: d.get(k) for k in ("id", "name", "alias", "user", "port", "transient")}

    # ---- the loop ----
    def run(self):
        self.kick("start")
        last_net = last_ts = 0
        while True:
            with self.cond:
                self.cond.wait_for(lambda: self.stopped or self.kicks, TICK)
                if self.stopped:
                    return
                reasons, self.kicks = self.kicks, []
                self.busy = bool(reasons)
            try:
                t = now()
                if t - last_net >= NETWORK_EVERY:
                    last_net = t
                    fp = frame_network.fingerprint()
                    if self.last_fp is not None and fp[::2] != self.last_fp[::2]:
                        reasons.append("network")
                    self.last_fp = fp
                    self.watch_config()
                    if self.state["phase"] == "connected" and self.control and self.master is None \
                            and not self.check(self.opts):
                        reasons.append("dropped")  # a master we found open, not one we started
                if t - last_ts >= TAILSCALE_EVERY and self.state["network"] and not reasons:
                    last_ts = t
                    self.refresh_network()
                phase = self.state["phase"]
                if phase == "connected" and not self.alive():
                    reasons.append("dropped")
                if phase == "failed" and self.state["retry_at"] and now() >= self.state["retry_at"]:
                    reasons.append("retry")
                if reasons:
                    self.connect(reasons)
            except Exception as e:  # keep the loop alive whatever happens; say what went wrong
                self.publish(phase="failed", error={"stage": "network", "message": f"{type(e).__name__}: {e}",
                                                    "raw": str(e)}, retry_at=now() + RETRY[-1])
            finally:
                with self.cond:
                    self.busy = False
                    self.cond.notify_all()

    def watch_config(self):
        """Set Up Connection may have added a headset or found a new address: pick it up."""
        try:
            mtime = frame_devices.ssh_config().stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self.config_mtime:
            self.config_mtime = mtime
            before = self.active_device()
            if before.get("transient") and not before.get("none") and self.routed is not None:
                # A bare alias is in use: a headset set up now doesn't take over by itself.
                self.override = self.override or before["alias"]
                self.session_alias = self.session_alias or before["alias"]  # and stays on the list
            if self.reg.sync_from_config():
                self.devices_changed()
                after = self.active_device()
                if (before.get("user"), before.get("port")) != (after.get("user"), after.get("port")):
                    self.deferred = True  # Set Up Connection changed the active headset's login
        if self.deferred:
            with self.work_lock:  # not while an install runs: it reads the route step by step
                if not self.work():
                    self.deferred = False
                    self.invalidate()

    def refresh_network(self):
        net = frame_network.current_network(self.last_fp)
        self.reg.record_network(net)
        net["name"] = self.reg.network_name(net)
        self.publish(network=net)

    # ---- one attempt ----
    def connect(self, reasons):
        why = self.describe(reasons)
        self.last_attempt = now()
        self.close_master()
        with self.work_lock, self.route_lock:
            gen = self.attempt_gen = self.gen
            device = self.active_device()
            if self.routed and self.routed[0] == device["id"] and self.route_key(device) != self.routed \
                    and self.work():
                # Its login changed in ~/.ssh/config while an install runs: reconnect with the
                # one it started with; the change applies once it's done (see watch_config).
                device = dict(device, user=self.routed[1], port=self.routed[2])
                self.deferred = True
            if self.route_key(device) != self.routed:
                # Another headset, or a new user or port: nothing may go on using the old
                # route, even if this attempt fails.
                self.alias, self.opts = device["alias"], self.first_route(device)
                self.apply(self.alias, self.opts)
                self.routed = self.route_key(device)
        with self.cond:
            self.state.update(phase="connecting", reason=why, device=self.public_device(device), via=None,
                              error=None, retry_at=None, attempt=self.state["attempt"] + 1, started=now(),
                              finished=None, probes=[],
                              stages=[{"id": i, "label": label, "state": "pending", "detail": "",
                                       "started": None, "ended": None} for i, label in STAGES])
            self.version += 1
            self.cond.notify_all()
        ok = False
        try:
            ok = self.attempt(device)
        finally:
            with self.cond:
                if gen != self.gen:
                    # The headset changed meanwhile: this attempt's result is about the
                    # old one. Leave "connecting"; the queued switch starts the next.
                    self.state["phase"] = "connecting"
                    self.version += 1
                    self.cond.notify_all()
                    ok = None
            if ok is None:
                self.close_master()
                return
            with self.cond:
                self.state["finished"] = now()
                if ok:
                    self.fails = 0
                    self.state.update(phase="connected", retry_at=None, error=None)
                else:
                    self.fails += 1
                    self.state.update(phase="failed", retry_at=None if device.get("none") or not (device.get("transient") or device["addresses"]) else
                                      now() + RETRY[min(self.fails, len(RETRY)) - 1])
                    if not self.state["error"]:
                        self.state["error"] = {"stage": "find", "message": "Couldn't connect", "raw": ""}
                self.version += 1
                self.cond.notify_all()

    @staticmethod
    def describe(reasons):
        for r in reasons:
            if r == "network":
                return "This computer changed networks"
            if r == "dropped" or r.startswith("lost"):
                return "The connection dropped"
            if r == "switch":
                return "Switched headset"
        if "retry" in reasons:
            return "Trying again"
        if "start" in reasons:
            return "Starting up"
        return "Connecting"

    def fail(self, sid, message, raw=""):
        self.stage(sid, "failed", message)
        with self.cond:
            self.state["error"] = {"stage": sid, "message": message, "raw": raw}

    def attempt(self, device):
        if device.get("none"):
            self.fail("find", "No headset is set up. Add one on the Devices tab.")
            return False
        if not device.get("transient") and not device["addresses"]:
            self.fail("find", f"{device['name']} has no addresses. Add one on the Devices tab.")
            return False
        # 1. this computer's network
        self.stage("network", "active")
        net = frame_network.current_network(self.last_fp)
        self.reg.record_network(net)
        net["name"] = self.reg.network_name(net)
        ts = net.get("tailscale") or {}
        self.publish(network=net)
        bits = [net["name"]]
        if net.get("local_ip"):
            bits.append(f"this computer is {net['local_ip']}")
        bits.append("Tailscale on" if ts.get("up") else "Tailscale off" if ts.get("installed") else "no Tailscale")
        self.stage("network", "done" if net.get("gateway") or ts.get("up") else "failed", " · ".join(bits))
        if not net.get("gateway") and not ts.get("up"):
            with self.cond:
                self.state["error"] = {"stage": "network", "raw": "",
                                       "message": "This computer isn't connected to a network."}
            # Keep going anyway: a headset on a direct link or loopback could still answer.

        # 2. find the headset
        self.stage("find", "active")
        port = device.get("port") or 22
        if device.get("transient"):
            host, port, user, proxied = ssh_g(device["alias"])
            if user and not device.get("user"):
                device["user"] = user
            a = {"host": host, "kind": frame_network.guess_kind(host), "label": "from ~/.ssh/config"}
            if proxied:
                # Reached through a jump host: only ssh itself can find it.
                with self.cond:
                    self.state["probes"] = [dict(a, why="through a jump host", state="answered", ip=None, rtt_ms=None,
                                                 detail="ssh's ProxyJump or ProxyCommand connects")]
                self.stage("find", "done", f"{device['alias']} goes through a jump host; ssh finds it")
                return self.handshake(device, a, {"ip": None, "rtt_ms": None}, device.get("user") or user) == "ok" \
                    and self.finish_bare(a, net)
            ranked = [(a, "from ~/.ssh/config")]
        else:
            ranked = frame_devices.order_addresses(device["addresses"], net.get("id"), bool(ts.get("up")))
        with self.cond:
            self.state["probes"] = [{"host": a["host"], "kind": a["kind"], "label": a.get("label") or "",
                                     "why": why, "state": "waiting", "detail": "Waiting", "ip": None,
                                     "rtt_ms": None} for a, why in ranked]
        self.stage("find", "active", f"Trying {len(ranked)} address{'es' * (len(ranked) != 1)} at once")
        results = [None] * len(ranked)
        done = threading.Condition()

        attempt_no = self.state["attempt"]

        def run_probe(i, host):
            res = probe(host, port, update=lambda **f: self.probe_update(i, attempt_no, **f))
            res.setdefault("ip", None)
            res.setdefault("rtt_ms", None)
            self.probe_update(i, attempt_no, **{k: res[k] for k in ("state", "detail", "ip", "rtt_ms")})
            with done:
                if results[i] is None:  # not already given up on
                    results[i] = dict(res, t=time.monotonic())
                done.notify_all()

        for i, (a, _) in enumerate(ranked):
            threading.Thread(target=run_probe, args=(i, a["host"]), daemon=True).start()

        tried = set()
        user = device.get("user") or "the headset's user"
        deadline = time.monotonic() + PROBE_TIMEOUT + 1
        while True:
            pick = self.pick(results, tried, done, deadline)
            if pick is None:
                break
            if tried:  # another address may do better (a different device answered, or it dropped)
                for sid in ("ssh", "identity", "login"):
                    self.stage(sid, "pending", "")
            tried.add(pick)
            a = ranked[pick][0]
            self.stage("find", "done", f"{a['host']} answered in {results[pick]['rtt_ms']:g} ms")
            outcome = self.handshake(device, a, results[pick], user)
            if outcome == "ok":
                via = {"host": a["host"], "kind": a["kind"], "ip": results[pick]["ip"],
                       "rtt_ms": results[pick]["rtt_ms"], "why": ranked[pick][1], "network": net.get("id"),
                       "network_name": net["name"]}
                self.publish(via=via)
                self.learn(device, a["host"], net, results[pick]["rtt_ms"])
                return True
            with self.cond:
                why_not = (self.state["error"] or {}).get("message") or "SSH failed"
            self.probe_update(pick, state="sshfailed", detail=why_not)
            if outcome != "next":
                return False
        if tried:
            return False  # the last handshake already said why
        # Nothing answered: explain with the most useful failure.
        with self.cond:
            for row in self.state["probes"]:
                if row["state"] in ("waiting", "resolving", "trying"):
                    row.update(state="timeout", detail="No answer in time")
        states = [r["state"] for r in results if r]
        worst = next((s for s in ("refused", "timeout", "unreachable", "unresolved") if s in states), "timeout")
        i = states.index(worst) if worst in states else 0
        raw = probe_raw(ranked[i][0]["host"], port, results[i] or {"state": worst})
        message = self.explain(raw) or "The Frame isn't answering."
        self.fail("find", message, raw)
        return False

    def finish_bare(self, a, net):
        self.publish(via={"host": a["host"], "kind": a["kind"], "ip": None, "rtt_ms": None,
                          "why": "through a jump host", "network": net.get("id"), "network_name": net["name"]})
        return True

    @staticmethod
    def pick(results, tried, done, deadline=None):
        """The next address to use: the best-ranked answer once every better-ranked
        address has failed, or once it has waited PREFER seconds for them. None when
        nothing (else) answered. Probes still going at `deadline` (a name lookup can
        take longer than the connect timeout) count as no answer."""
        deadline = deadline or time.monotonic() + PROBE_TIMEOUT + 1
        with done:
            while True:
                if time.monotonic() >= deadline:
                    for i, r in enumerate(results):
                        if r is None:
                            results[i] = {"state": "timeout", "detail": "No answer", "ip": None, "rtt_ms": None,
                                          "t": time.monotonic()}
                answered = [i for i, r in enumerate(results) if r and r["state"] == "answered" and i not in tried]
                pending = [i for i, r in enumerate(results) if r is None]
                if answered:
                    best = answered[0]
                    better = [i for i in pending if i < best]
                    waited = time.monotonic() - results[best]["t"]
                    if not better or waited >= PREFER:
                        return best
                    done.wait(PREFER - waited)
                elif not pending:
                    return None
                else:
                    done.wait(min(0.5, max(0.01, deadline - time.monotonic())))

    def learn(self, device, host, net, rtt):
        if device.get("transient"):
            return
        self.reg.record_success(device["id"], host, net.get("id"), rtt)
        # Set Up Connection may have changed the block while this attempt ran: take that
        # in (and reconnect) rather than writing this attempt's older settings over it.
        self.watch_config()
        try:
            now_dev = self.reg.get(device["id"])
        except frame_devices.DeviceError:
            return
        if self.route_key(now_dev) != self.route_key(device):
            return
        # Terminal's `ssh ALIAS` and the helper scripts use ~/.ssh/config: point it here too,
        # unless the block changed since this attempt began (checked under the file lock).
        login = device.get("config_login") or [None, None]
        expect = {"hostname": device.get("config_host"), "user": login[0], "port": login[1]}
        try:
            if frame_devices.rewrite_block(device["alias"], hostname=host, user=device["user"],
                                           port=device["port"], expect=expect):
                self.config_mtime = frame_devices.ssh_config().stat().st_mtime
                self.reg.set_config_host(device["id"], host)
                self.reg.sync_from_config()  # records the login it now holds
        except OSError:
            pass  # not fatal: the app itself doesn't need the file
        self.devices_changed()

    # ---- SSH ----
    def check(self, opts, alias=None):
        """Is a master connection up for these options? (`ssh -O check`)"""
        if not self.control:
            return False
        try:
            return subprocess.run([*self.mux_base, *opts, "-O", "check", alias or self.alias], capture_output=True,
                                  stdin=subprocess.DEVNULL, timeout=5).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def close_master(self):
        proc, self.master = self.master, None
        pending, self.pending = self.pending, None
        if pending and pending.poll() is None:
            pending.kill()
        if self.control and self.alias:
            try:
                subprocess.run([*self.mux_base, *self.opts, "-O", "exit", self.alias], capture_output=True,
                               stdin=subprocess.DEVNULL, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def handshake(self, device, a, found, user):
        """SSH to one address, following ssh -v through stages 3-5.
        -> "ok", "next" (try another address) or "stop"."""
        # The IP that answered (with a link-local IPv6 address's zone), so ssh doesn't
        # look the name up again and stall on an address that didn't answer.
        opts = self.host_opts(device, ssh_target(a["host"], found.get("ip")))
        alias = device["alias"]
        with self.route_lock:
            if self.attempt_gen != self.gen:
                return "stop"  # the headset changed: don't route anything back to this one
            self.alias, self.opts = alias, opts
            self.apply(alias, opts)
        target = f"{a['host']}" + (f" ({found['ip']})" if found.get("ip") and found["ip"] != a["host"] else "")
        self.stage("ssh", "active", f"Opening SSH to {target}")
        if self.control and self.check(opts, alias):
            for sid in ("ssh", "identity", "login"):
                self.stage(sid, "done", "Reusing the SSH connection that's already open")
            return "ok"
        extra = []
        if not device.get("transient"):
            if frame_devices.pinned(device["id"]):
                extra = ["-o", "StrictHostKeyChecking=yes"]
            else:
                # First connection since this headset was added: trust what it shows
                # (as Set Up Connection does), and pin it from now on. ssh won't create
                # the folder its known_hosts file goes in.
                extra = ["-o", "StrictHostKeyChecking=accept-new"]
                pins = frame_devices.known_hosts(device["id"]).parent
                pins.mkdir(**({} if frame_host.WINDOWS else {"mode": 0o700}), parents=True, exist_ok=True)
        if self.control:
            # No ConnectTimeout: with it, OpenSSH's master takes ~5s to open its socket.
            argv = [*self.mux_base, *opts, *extra, "-v", "-o", "ControlMaster=yes", "-o", "ServerAliveInterval=5",
                    "-o", "ServerAliveCountMax=2", "-N", alias]
        else:
            argv = [*self.mux_base, *opts, *extra, "-v", "-o", "ConnectTimeout=10", alias, "true"]
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, **frame_host.DETACHED)
        except OSError as e:
            self.fail("ssh", f"Couldn't run ssh: {e}", str(e))
            return "stop"
        self.pending = proc  # so stop() can end it mid-handshake
        lines = queue.Queue()
        collecting = [True]

        def read():
            for raw in iter(proc.stderr.readline, b""):
                if collecting[0]:
                    lines.put(raw.decode("utf-8", "replace").rstrip())
            lines.put(None)
            proc.stderr.close()
        threading.Thread(target=read, daemon=True).start()

        step, said, authed = "ssh", [], False
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT
        mismatch = False
        while True:
            left = deadline - time.monotonic()
            if self.stopped:
                proc.kill()
                return "stop"
            if left <= 0:
                proc.kill()
                self.fail(step, self.explain(f"Timed out talking to {alias}") or "The headset took too long to answer.",
                          f"Timed out talking to {alias}")
                return "next" if step in ("ssh", "identity") else "stop"
            try:
                line = lines.get(timeout=min(left, 0.25))
            except queue.Empty:
                line = ""
                if authed and self.control and self.check(opts, alias):
                    break
                if proc.poll() is not None and lines.empty():
                    line = None
                else:
                    continue
            if line is None:  # ssh exited
                proc.wait()
                if not self.control and proc.returncode == 0:
                    break
                if authed and self.control and self.check(opts, alias):
                    break
                return self.failed(step, said, mismatch, alias)
            if not line.startswith("debug"):
                said.append(line)
            m = CONNECTING.search(line)
            if m:
                self.stage("ssh", "active", f"Opening SSH to {a['host']}" +
                           (f" ({m.group(2)})" if m.group(2) != a["host"] else "") + f", port {m.group(3)}")
            elif ESTABLISHED.search(line):
                self.stage("ssh", "done", f"Connected to {target}")
                step = "identity"
                self.stage("identity", "active", "Waiting for the headset's host key")
            elif HOSTKEY.search(line):
                m = HOSTKEY.search(line)
                self.stage("identity", "active", f"It shows {m.group(1)} key {m.group(2)[:20]}…")
            elif KNOWN.search(line):
                self.stage("identity", "done", "Matches the identity saved for this headset")
                step = "login"
                self.stage("login", "active", f"Logging in as {user}")
            elif ADDED.search(line):
                self.stage("identity", "done", "First connection: saved this headset's identity")
                step = "login"
                self.stage("login", "active", f"Logging in as {user}")
            elif (CHANGED.search(line) or UNKNOWN.search(line)) and not device.get("transient"):
                mismatch = True  # a bare alias keeps ssh's per-address check, and its wording
            elif AUTH_START.search(line) and step != "login":
                self.stage("identity", "done")
                step = "login"
                self.stage("login", "active", f"Logging in as {user}")
            elif AUTHED.search(line):
                authed = True
                if step != "login":
                    self.stage("identity", "done")
                self.stage("login", "done", f"Logged in as {user}")
                step = "connected"
        collecting[0] = False  # the master keeps printing mux debug lines: drop them
        self.pending = None
        if self.stopped:
            proc.kill()
            return "stop"
        for sid in ("ssh", "identity", "login"):
            with self.cond:
                pending = any(s["id"] == sid and s["state"] != "done" for s in self.state["stages"])
            if pending:
                self.stage(sid, "done")
        if self.control:
            self.master = proc
        return "ok"

    def failed(self, step, said, mismatch, alias):
        text = "\n".join(said).strip()
        if mismatch:
            self.fail("identity", "This address answered as a different headset (its SSH identity doesn't match). "
                      "If SteamOS was reinstalled, use Forget Identity on the Devices tab.", text)
            return "next"
        if step == "login" or re.search(r"Permission denied", text):
            self.fail("login", self.explain(text) or "The headset didn't accept this computer's key.", text)
            return "stop"
        if step == "connected":
            self.fail("login", "Logged in, but the shared SSH connection didn't start.", text)
            return "stop"
        self.fail(step, self.explain(text) or (text.splitlines()[-1] if text else "ssh stopped"), text)
        return "next"

    # ---- Test now ----
    def test(self, device_id):
        """Probe every address of a headset and check SSH on the ones that answer,
        without touching the live connection. Results stream into state["tests"]."""
        device = self.reg.get(device_id)
        started = now()
        rows = [{"host": a["host"], "kind": a["kind"], "state": "waiting", "detail": "Waiting", "ip": None,
                 "rtt_ms": None, "ssh": None} for a in device["addresses"]]

        def put(**fields):
            with self.cond:
                self.state["tests"][device_id] = dict({"started": started, "done": False, "rows": rows}, **fields)
                self.version += 1
                self.cond.notify_all()

        put()
        net = self.state["network"] or {}

        def one(i, a):
            res = probe(a["host"], device["port"], update=lambda **f: (rows[i].update(f), put()))
            rows[i].update({k: res.get(k) for k in ("state", "detail", "ip", "rtt_ms")})
            put()
            if res["state"] != "answered":
                return
            rows[i]["ssh"] = "checking"
            put()
            argv = [*self.mux_base[:3], "-o", "ControlPath=none", "-o", "ConnectTimeout=8",
                    *self.host_opts(device, ssh_target(a["host"], res.get("ip"))),
                    "-o", "StrictHostKeyChecking=yes", device["alias"], "true"]
            try:
                r = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, text=True,
                                   errors="replace", timeout=20)
                err = r.stderr.strip()
                if r.returncode == 0:
                    rows[i].update(ssh="ok", detail=f"Answered in {res['rtt_ms']:g} ms · SSH works")
                    self.reg.record_success(device_id, a["host"], net.get("id"), res["rtt_ms"])
                elif UNKNOWN.search(err):
                    rows[i].update(ssh="unpinned", detail=f"Answered in {res['rtt_ms']:g} ms · identity not saved yet")
                elif CHANGED.search(err):
                    rows[i].update(ssh="wrong", detail="Answered as a different headset")
                elif DENIED.search(err):
                    rows[i].update(ssh="denied", detail="Answered, but refused this computer's key")
                else:
                    rows[i].update(ssh="failed", detail=self.explain(err) or (err.splitlines() or ["SSH failed"])[-1])
            except (OSError, subprocess.TimeoutExpired):
                rows[i].update(ssh="failed", detail="SSH took too long")
            put()

        threads = [threading.Thread(target=one, args=(i, a), daemon=True) for i, a in enumerate(device["addresses"])]
        for t in threads:
            t.start()
        for t in threads:
            t.join(40)
        put(done=True, finished=now())
        self.devices_changed()


# ---- the page's API: /api/devices -------------------------------------------------

def devices_view(link):
    """Every headset with its addresses, the networks they worked on, and the current network."""
    snap = link.reg.snapshot()
    active = link.active_device()
    names = {nid: link.reg.network_name(dict(n, id=nid)) for nid, n in snap["networks"].items()}
    devices = []
    bare = link.bare(link.session_alias) if link.session_alias and not link.reg.by_alias(link.session_alias) else None
    for extra in ([active] if active.get("transient") and not active.get("none") else []) + \
            ([bare] if bare and bare["id"] != active["id"] else []):
        devices.append(dict(link.public_device(extra), active=extra["id"] == active["id"], addresses=[],
                            managed=False, pinned=False))
    for d in snap["devices"]:
        view = {k: v for k, v in d.items() if k not in ("config_host", "addresses")}
        view["active"] = d["id"] == active["id"]
        view["pinned"] = frame_devices.pinned(d["id"])
        view["addresses"] = [dict(a, network_names=[names.get(n, "an unnamed network") for n in a["networks"]])
                             for a in d["addresses"]]
        devices.append(view)
    return {"devices": devices, "active": active["id"], "network": link.state["network"],
            "networks": [dict(n, id=nid, display=names[nid]) for nid, n in snap["networks"].items()],
            "kinds": frame_devices.KIND_LABEL}


def devices_action(link, body, open_setup, busy=lambda: 0):
    """POST /api/devices {"action": ..., "id": device id, ...}. -> {"message", ...devices_view}.
    busy() counts installs in progress: nothing may move them to another headset."""
    reg = link.reg
    action = body.get("action")
    did = body.get("id")
    active = link.active_device()
    is_active = did == active["id"]
    moves = action == "use" or (is_active and (
        action in ("remove", "address-remove")
        or (action == "update" and (body.get("user") is not None or body.get("port") is not None))
        or (action == "address-update" and body.get("newHost") not in (None, body.get("host")))))
    if moves and busy():
        raise frame_devices.DeviceError(
            f"Wait for what's running on {active['name']} to finish (see the activity bar), then try again")
    if action == "use":
        sa = link.session_alias
        d = link.bare(sa) if sa and did == link.bare(sa)["id"] and not reg.by_alias(sa) else reg.get(did)
        link.use(did)
        msg = f"Switched to {d['name']}"
    elif action == "update":
        before = reg.get(did)
        d = reg.update_device(did, name=body.get("name"), user=body.get("user"), port=body.get("port"))
        login_changed = (d["user"], d["port"]) != (before["user"], before["port"])
        if is_active and login_changed:
            link.invalidate()  # before anything else can fail: the old login mustn't stay in use
        msg = f"Saved {d['name']}"
        if login_changed:
            # Only what changed, and only if the block still says what it did: Set Up
            # Connection may have written a new login meanwhile, which then stands.
            try:
                if not frame_devices.rewrite_block(d["alias"], user=d["user"], port=d["port"],
                                                   expect={"user": before["user"], "port": before["port"]}) \
                        and any(b["alias"] == d["alias"] and (b["user"], b["port"]) != (d["user"], d["port"])
                                for b in frame_devices.parse_blocks(frame_devices.read_config())):
                    msg += "; ~/.ssh/config changed meanwhile, so it was left as it is"
            except OSError as e:
                raise frame_devices.DeviceError(f"Saved, but couldn't update ~/.ssh/config: {e}")
    elif action == "remove":
        if not body.get("config") and len(reg.devices()) == 1 and reg.get(did)["alias"] in {
                b["alias"] for b in frame_devices.parse_blocks(frame_devices.read_config())}:
            # Its ssh alias would stay, and the app would go on using it as a bare alias.
            raise frame_devices.DeviceError("This is your only headset. To remove it completely, also remove its "
                                            "entry from ~/.ssh/config (the box below)")
        d = reg.remove_device(did)
        if is_active:
            link.override = None
            link.invalidate()
        frame_devices.forget_pin(did)
        removed = False
        if body.get("config"):
            try:
                removed = frame_devices.remove_block(d["alias"])
            except OSError as e:
                raise frame_devices.DeviceError(f"Removed, but couldn't edit ~/.ssh/config: {e}")
        msg = f"Removed {d['name']}" + (f" and its '{d['alias']}' entry in ~/.ssh/config" if removed else "")
    elif action == "address-add":
        a = reg.add_address(did, body.get("host"), body.get("kind") or None, body.get("label") or "")
        if is_active and link.state["phase"] == "failed":
            link.kick("retry")
        msg = f"Added {a['host']}"
    elif action == "address-update":
        a = reg.update_address(did, body.get("host"), new_host=body.get("newHost"), kind=body.get("kind"),
                               label=body.get("label"))
        if is_active and a["host"] != body.get("host"):
            link.invalidate()  # the address in use may have moved
        msg = f"Saved {a['host']}"
    elif action == "address-remove":
        reg.remove_address(did, body.get("host"))
        if is_active:
            link.invalidate()  # it may be the address in use: stop using it now
        msg = f"Removed {body.get('host')}"
    elif action == "address-move":
        delta = body.get("delta")
        if delta not in (-1, 1):
            raise frame_devices.DeviceError("delta must be -1 or 1")
        reg.move_address(did, body.get("host"), delta)
        msg = "Moved"
    elif action == "test":
        d = reg.get(did)
        if not d["addresses"]:
            raise frame_devices.DeviceError("This headset has no addresses to test yet")
        threading.Thread(target=link.test, args=(did,), daemon=True).start()
        msg = f"Testing {len(d['addresses'])} address{'es' * (len(d['addresses']) != 1)}"
    elif action == "forget-identity":
        d = reg.get(did)
        frame_devices.forget_pin(did)
        if is_active:
            link.kick("switch")
        msg = f"Forgot {d['name']}'s SSH identity; the next connection saves the one it shows"
    elif action == "name-network":
        reg.name_network(body.get("network"), body.get("name"))
        if link.state["network"]:
            link.refresh_network()
        msg = "Saved the network's name"
    elif action == "setup":
        alias = frame_devices.check_alias(body.get("alias"))
        host = frame_devices.check_host(body["host"]) if body.get("host") else None
        link.reg.undismiss(alias)
        where = open_setup(alias, host)
        msg = f"Opened Set Up Connection for '{alias}' in {where}"
    elif action == "retry":
        link.kick("retry")
        msg = "Connecting…"
    else:
        raise frame_devices.DeviceError("unknown action")
    link.devices_changed()
    return dict(devices_view(link), message=msg)


def next_alias(link):
    taken = {d["alias"] for d in link.reg.devices()} | {b["alias"] for b in frame_devices.parse_blocks(
        frame_devices.read_config())}
    if "frame" not in taken:
        return "frame"
    n = 2
    while f"frame-{n}" in taken:
        n += 1
    return f"frame-{n}"


LIKELY = re.compile(r"frame|steam", re.I)


def tailscale_find(link, device_id=None):
    """Tailscale peers that could be a headset, likely ones first, for "Find on Tailscale"."""
    ts = frame_network.tailscale_status()
    device = link.reg.get(device_id) if device_id else link.active_device()
    known = {a["host"].rstrip(".").lower() for a in device.get("addresses") or []}
    if not ts.get("installed"):
        return {"up": False, "peers": [], "message": "Tailscale isn't installed on this computer."}
    if not ts.get("up"):
        return {"up": False, "peers": [], "message": "Tailscale isn't running on this computer. Start it, then look again."}
    peers = []
    for p in ts["peers"]:
        ip = next((i for i in p["ips"] if "." in i), p["ips"][0] if p["ips"] else None)
        likely = p["os"] == "linux" and (LIKELY.search(p["name"]) or p["name"].lower() in (
            device["alias"].lower(), (device.get("name") or "").lower()))
        peers.append({"name": p["name"], "dns": p["dns"], "ip": ip, "os": p["os"], "online": p["online"],
                      "likely": bool(likely), "added": bool({p["dns"].lower(), (ip or "").lower()} & known)})
    peers.sort(key=lambda p: (not p["likely"], p["os"] != "linux", not p["online"], p["name"].lower()))
    return {"up": True, "peers": peers, "tailnet": ts.get("tailnet"), "message": None}


def mdns_find(link, device_id=None):
    """Headsets on this network: SteamOS devkit services (mDNS) and <alias>.local."""
    device = link.reg.get(device_id) if device_id else link.active_device()
    known = {a["host"].rstrip(".").lower() for a in device.get("addresses") or []}
    try:
        import frame_connect
        found = frame_connect.discover_devkit()
    except (ImportError, SystemExit, OSError):
        found = []
    names = list(dict.fromkeys([h.rstrip(".") for h in found] + [f"{device['alias']}.local", "frame.local"]))
    rows = [None] * len(names)

    def check(i, host):
        res = probe(host, 22, timeout=3)
        rows[i] = {"host": host, "state": res["state"], "ip": res.get("ip"), "rtt_ms": res.get("rtt_ms"),
                   "detail": res["detail"], "advertised": host in [h.rstrip(".") for h in found],
                   "added": host.lower() in known or (res.get("ip") or "").lower() in known}
    threads = [threading.Thread(target=check, args=(i, h), daemon=True) for i, h in enumerate(names)
               if frame_devices.HOST_RE.fullmatch(h)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(8)
    hosts = [r for r in rows if r and (r["advertised"] or r["state"] in ("answered", "refused"))]
    return {"hosts": hosts, "tool": bool(frame_host.which("dns-sd") or frame_host.which("avahi-browse"))}
