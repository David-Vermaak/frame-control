"""frame_link: finding a headset among its addresses and following each stage of
connecting, with a stand-in ssh (tests/fakessh/ssh) and real sockets on this computer.
Also the server's /api/connection, its event stream, and /api/devices.

Run: python3 -m unittest discover -s tests
"""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import frame_devices as fd  # noqa: E402
import frame_link as fl  # noqa: E402
import frame_network as fn  # noqa: E402

FAKESSH = ROOT / "tests" / "fakessh"
NET = {"id": "n-test", "gateway": "192.168.1.1", "gateway_mac": "aa:bb:cc:dd:ee:ff", "interface": "en0",
       "ssid": None, "wifi": True, "local_ip": "192.168.1.9", "tailscale": {"up": False, "installed": False}}


def explain(msg):
    """A cut-down server.unreachable, so this needs no server import."""
    if "Could not resolve" in msg:
        return "Can't find the Frame on the network."
    if "refused" in msg:
        return "The Frame refused the connection."
    if "timed out" in msg.lower():
        return "The Frame isn't answering."
    if "Permission denied" in msg:
        return "The Frame didn't accept this computer's SSH key."
    return None


class Probe(unittest.TestCase):
    def test_answers_refusals_and_unknown_names(self):
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(4)
            port = srv.getsockname()[1]
            seen = []
            res = fl.probe("127.0.0.1", port, update=lambda **f: seen.append(f["state"]))
            self.assertEqual(res["state"], "answered")
            self.assertEqual(res["ip"], "127.0.0.1")
            self.assertIsInstance(res["rtt_ms"], float)
            self.assertEqual(seen, ["resolving", "trying"])
        self.assertEqual(fl.probe("127.0.0.1", port, timeout=2)["state"], "refused")  # closed now
        self.assertEqual(fl.probe("frame-control-test.invalid", 22, timeout=2)["state"], "unresolved")

    def test_failed_probes_read_like_ssh(self):
        # So the server's UNREACHABLE table words them like any other ssh failure.
        self.assertIn("Could not resolve hostname x", fl.probe_raw("x", 22, {"state": "unresolved"}))
        self.assertIn("port 22: Connection refused", fl.probe_raw("x", 22, {"state": "refused"}))
        self.assertIn("Operation timed out", fl.probe_raw("x", 22, {"state": "timeout"}))


class Pick(unittest.TestCase):
    def pick(self, results, tried=()):
        return fl.Link.pick(results, set(tried), threading.Condition(), time.monotonic() + 5)

    def test_best_ranked_answer_wins(self):
        now = time.monotonic()
        ok = lambda t=now: {"state": "answered", "t": t}
        no = {"state": "timeout", "t": now}
        self.assertEqual(self.pick([no, ok(), ok()]), 1)
        self.assertEqual(self.pick([no, ok(), ok()], tried=[1]), 2)
        self.assertIsNone(self.pick([no, no]))
        # A worse-ranked answer waits PREFER for a better one still trying, then goes.
        t0 = time.monotonic()
        self.assertEqual(self.pick([None, ok(time.monotonic())]), 1)
        self.assertGreaterEqual(time.monotonic() - t0, fl.PREFER - 0.05)

    def test_gives_up_on_slow_lookups_at_the_deadline(self):
        t0 = time.monotonic()
        results = [None]
        self.assertIsNone(fl.Link.pick(results, set(), threading.Condition(), time.monotonic() + 0.3))
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(results[0]["state"], "timeout")


@unittest.skipIf(os.name == "nt", "the stand-in ssh is a POSIX script")
class Connecting(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="frame-link-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (self.dir / "ssh").mkdir()
        self.log = self.dir / "calls.jsonl"
        env = {"FRAME_CONTROL_SSH_DIR": str(self.dir / "ssh"), "FAKESSH_LOG": str(self.log),
               "FAKESSH_DIR": str(self.dir), "PATH": f"{FAKESSH}{os.pathsep}{os.environ['PATH']}"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("current_network", lambda *a, **k: dict(NET)), ("fingerprint", lambda: ("192.168.1.1", "en0", "aa:bb:cc:dd:ee:ff"))):
            p = mock.patch.object(fn, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(16)
        self.addCleanup(self.srv.close)
        self.port = self.srv.getsockname()[1]
        self.reg = fd.Registry(self.dir / "devices.json")
        self.routes = []
        self.link = fl.Link(self.reg, env_alias=None, mux_base=["ssh", "-o", "BatchMode=yes", "-o", "ControlPath=x"],
                            control="x", apply=lambda alias, opts: self.routes.append((alias, list(opts))),
                            explain=explain)
        self.addCleanup(self.link.stop)

    def hosts(self, mapping):
        os.environ["FAKESSH_HOSTS"] = json.dumps(mapping)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def device(self, *hosts):
        d = self.reg.add_device("frame-t", port=self.port, hosts=[])
        for h in hosts:
            self.reg.add_address(d["id"], h, kind="lan")
        return d

    def test_falls_through_to_the_address_that_is_really_the_headset(self):
        # Tried in this order: a name that doesn't resolve, a different device, the headset.
        d = self.device("nothing.invalid", "127.0.0.1", "localhost")
        self.hosts({"127.0.0.1": "wrong", "localhost": "ok"})
        self.link.connect(["start"])
        s = self.link.snapshot()
        self.assertEqual(s["phase"], "connected", s["error"])
        self.assertEqual(s["via"]["host"], "localhost")
        self.assertEqual([st["state"] for st in s["stages"]], ["done"] * 5)
        rows = {p["host"]: p for p in s["probes"]}
        self.assertEqual(rows["nothing.invalid"]["state"], "unresolved")
        self.assertEqual(rows["127.0.0.1"]["state"], "sshfailed")
        self.assertIn("different headset", rows["127.0.0.1"]["detail"])
        # Every ssh command was pointed at the winner, with the host key pinned per device.
        alias, opts = self.routes[-1]
        self.assertEqual(alias, "frame-t")
        self.assertIn(f"HostName=localhost", opts)
        self.assertIn(f"HostKeyAlias=frame-control-{d['id']}", opts)
        self.assertIn(f"Port={self.port}", opts)
        master = [c for c in self.calls() if "ControlMaster=yes" in c][-1]
        self.assertIn("StrictHostKeyChecking=accept-new", master)  # first connection: nothing pinned yet
        # It learned: localhost works on this network.
        learned = {a["host"]: a for a in self.reg.get(d["id"])["addresses"]}
        self.assertEqual(learned["localhost"]["networks"], ["n-test"])
        self.assertEqual(learned["127.0.0.1"]["networks"], [])
        self.assertTrue(self.link.alive())
        self.link.close_master()
        self.assertFalse(any(p.name.startswith("master-") for p in self.dir.iterdir()))

    def test_stages_are_published_as_they_happen(self):
        self.device("localhost")
        self.hosts({"localhost": "ok"})
        steps, versions = [], []
        real = self.link.stage

        def stage(sid, state, detail=None):
            real(sid, state, detail)
            steps.append((sid, state))
            versions.append(self.link.snapshot()["version"])
        self.link.stage = stage
        before = self.link.snapshot()["version"]
        self.assertIsNone(self.link.wait(before, 0.05))  # nothing new yet
        self.link.connect(["start"])
        started = [sid for sid, state in steps if state == "active"]
        self.assertEqual(list(dict.fromkeys(started)), ["network", "find", "ssh", "identity", "login"])
        self.assertEqual([sid for sid, state in steps if state == "done"][-3:], ["ssh", "identity", "login"])
        self.assertEqual(versions, sorted(versions))  # every step is a new version for the page
        self.assertEqual(self.link.wait(before, 1)["phase"], "connected")

    def test_nothing_answers(self):
        self.device("nothing.invalid", "also-nothing.invalid")
        self.link.connect(["start"])
        s = self.link.snapshot()
        self.assertEqual(s["phase"], "failed")
        self.assertEqual(s["error"]["stage"], "find")
        self.assertEqual(s["error"]["message"], "Can't find the Frame on the network.")
        self.assertGreater(s["retry_at"], time.time())
        self.assertEqual([st["state"] for st in s["stages"]][:2], ["done", "failed"])

    def test_refused_key_stops_at_login(self):
        self.device("localhost")
        self.hosts({"localhost": "denied"})
        self.link.connect(["start"])
        s = self.link.snapshot()
        self.assertEqual((s["phase"], s["error"]["stage"]), ("failed", "login"))
        self.assertIn("SSH key", s["error"]["message"])

    def test_pinned_identity_is_checked_strictly(self):
        d = self.device("localhost")
        (self.dir / "ssh" / "frame-control_known_hosts").write_text(f"frame-control-{d['id']} ssh-ed25519 AAAA\n")
        self.hosts({"localhost": "ok"})
        self.link.connect(["start"])
        master = [c for c in self.calls() if "ControlMaster=yes" in c][-1]
        self.assertIn("StrictHostKeyChecking=yes", master)

    def test_a_bare_alias_lets_ssh_config_decide(self):
        self.link.override = "frame-bare"
        self.hosts({"frame-bare": "ok"})  # the stand-in ssh has no config: the alias is the host
        with mock.patch.object(fl, "ssh_g", return_value=("localhost", self.port, "tester", False)):
            self.link.connect(["start"])
        s = self.link.snapshot()
        self.assertEqual(s["phase"], "connected", s["error"])
        self.assertTrue(s["device"]["transient"])
        self.assertEqual(self.routes[-1], ("frame-bare", []))  # no HostName override, ssh's own known_hosts

    def test_a_bare_alias_behind_a_jump_host_is_left_to_ssh(self):
        self.link.override = "frame-jump"
        self.hosts({"frame-jump": "ok"})
        with mock.patch.object(fl, "ssh_g", return_value=("10.99.99.99", 22, "tester", True)):
            self.link.connect(["start"])
        s = self.link.snapshot()
        self.assertEqual(s["phase"], "connected", s["error"])
        self.assertEqual(s["via"]["why"], "through a jump host")

    def test_changing_the_port_reroutes_even_if_the_attempt_fails(self):
        d = self.device("localhost")
        self.hosts({"localhost": "ok"})
        self.link.connect(["start"])
        self.reg.update_device(d["id"], port=1)  # nothing listens there
        self.link.connect(["switch"])
        self.assertEqual(self.link.snapshot()["phase"], "failed")
        self.assertIn("Port=1", self.routes[-1][1])

    def test_test_now_checks_every_address_without_touching_the_connection(self):
        d = self.device("127.0.0.1", "localhost", "nothing.invalid")
        (self.dir / "ssh" / "frame-control_known_hosts").write_text(f"frame-control-{d['id']} ssh-ed25519 AAAA\n")
        self.hosts({"127.0.0.1": "wrong", "localhost": "ok"})
        self.link.test(d["id"])
        rows = {r["host"]: r for r in self.link.snapshot()["tests"][d["id"]]["rows"]}
        self.assertEqual(rows["localhost"]["ssh"], "ok")
        self.assertEqual(rows["127.0.0.1"]["ssh"], "wrong")
        self.assertEqual(rows["nothing.invalid"]["state"], "unresolved")
        self.assertEqual(self.routes, [])
        self.assertTrue(all("ControlPath=none" in c for c in self.calls()))

    def test_switching_to_a_headset_that_never_answers_stops_using_the_last_one(self):
        self.device("localhost")
        self.hosts({"localhost": "ok"})
        self.link.connect(["start"])
        other = self.reg.add_device("frame-other", port=self.port)
        self.reg.add_address(other["id"], "nothing.invalid")
        self.link.use(other["id"])
        self.assertEqual(self.routes[-1][0], "frame-other")  # at once, before any attempt
        self.assertEqual(self.link.snapshot()["phase"], "connecting")
        self.assertFalse(self.link.alive())  # so ensure() waits instead of using the old master
        self.link.connect(["switch"])
        self.assertEqual(self.link.snapshot()["phase"], "failed")
        alias, opts = self.routes[-1]
        self.assertEqual(alias, "frame-other")
        self.assertIn("HostName=nothing.invalid", opts)
        self.assertIn(f"HostKeyAlias=frame-control-{other['id']}", opts)

    def test_no_switching_while_something_is_installing(self):
        d = self.device("localhost")
        other = self.reg.add_device("frame-other")
        for body in ({"action": "use", "id": other["id"]}, {"action": "remove", "id": d["id"]},
                     {"action": "update", "id": d["id"], "port": 2222}):
            with self.assertRaises(fd.DeviceError, msg=body):
                fl.devices_action(self.link, body, open_setup=None, busy=lambda: 1)
        # Renaming, or changing another headset, is fine.
        fl.devices_action(self.link, {"action": "update", "id": d["id"], "name": "Desk"}, None, busy=lambda: 1)
        fl.devices_action(self.link, {"action": "update", "id": other["id"], "port": 2222}, None, busy=lambda: 1)
        self.assertEqual(self.reg.get(d["id"])["name"], "Desk")

    def test_stopping_mid_handshake_leaves_no_ssh_behind(self):
        self.device("localhost")
        self.hosts({"localhost": "slow"})
        t = threading.Thread(target=self.link.connect, args=(["start"],), daemon=True)
        t.start()
        for _ in range(100):
            if self.link.pending:
                break
            time.sleep(0.05)
        proc = self.link.pending
        self.assertIsNotNone(proc)
        self.link.stop()
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertIsNotNone(proc.poll())
        self.assertIsNone(self.link.master)

    def test_devices_api_checks_everything(self):
        d = self.device("localhost")
        bad = [{"action": "address-add", "id": d["id"], "host": "-oProxyCommand=touch /tmp/x"},
               {"action": "address-add", "id": d["id"], "host": "a\nHost *"},
               {"action": "address-add", "id": d["id"], "host": "frame.local", "kind": "wifi"},
               {"action": "update", "id": d["id"], "user": "root; id"},
               {"action": "update", "id": d["id"], "port": 0},
               {"action": "address-move", "id": d["id"], "host": "localhost", "delta": 5},
               {"action": "setup", "alias": "-F/etc/passwd"},
               {"action": "setup", "alias": "frame-9", "host": "$(id)"},
               {"action": "use", "id": "nope"},
               {"action": "explode"}]
        for body in bad:
            with self.assertRaises(fd.DeviceError, msg=body):
                fl.devices_action(self.link, body, open_setup=lambda *a: self.fail("setup ran"))
        opened = []
        out = fl.devices_action(self.link, {"action": "setup", "alias": "frame-9", "host": "192.168.1.50"},
                                open_setup=lambda alias, host: opened.append((alias, host)) or "a terminal")
        self.assertEqual(opened, [("frame-9", "192.168.1.50")])
        self.assertIn("frame-9", out["message"])
        self.assertEqual(out["active"], d["id"])
        self.assertEqual(fl.next_alias(self.link), "frame")


@unittest.skipIf(os.name == "nt", "the stand-in ssh is a POSIX script")
class ServerConnection(unittest.TestCase):
    """The real server, a Set Up Connection block in a stand-in ~/.ssh, and the stand-in ssh."""

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="frame-link-server-"))
        ssh_dir = cls.dir / "ssh"
        ssh_dir.mkdir()
        cls.srv = socket.socket()  # the "headset's" port 22
        cls.srv.bind(("127.0.0.1", 0))
        cls.srv.listen(16)
        (ssh_dir / "config").write_text("# >>> steam-frame (frame) >>>\nHost frame\n  HostName localhost\n"
                                        f"  Port {cls.srv.getsockname()[1]}\n"
                                        "  User steamos\nHost *\n# <<< steam-frame (frame) <<<\n")
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "FRAME_CONTROL_SSH_DIR": str(ssh_dir),
               "FRAME_CONTROL_DATA_DIR": str(cls.dir / "data"), "FAKESSH_LOG": str(cls.dir / "calls.jsonl"),
               "FAKESSH_DIR": str(cls.dir), "FAKESSH_HOSTS": json.dumps({"localhost": "ok"}),
               "PATH": f"{FAKESSH}{os.pathsep}{os.environ['PATH']}"}
        env.pop("FRAME_ALIAS", None)
        cls.log = tempfile.TemporaryFile()
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "ui" / "server.py"), "--port", "0"], env=env,
                                    stdout=subprocess.PIPE, stderr=cls.log, text=True)
        cls.port = int(cls.proc.stdout.readline().split("127.0.0.1:")[1].split()[0])

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=15)
        cls.proc.stdout.close()
        cls.log.close()
        cls.srv.close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def request(self, method, path, body=None, key="1"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                     headers={"X-Frame-UI": key, "Content-Type": "application/json"})
        r = conn.getresponse()
        data = json.loads(r.read() or b"{}")
        conn.close()
        return r.status, data

    def wait_connected(self):
        for _ in range(100):
            status, s = self.request("GET", "/api/connection")
            if s.get("phase") in ("connected", "failed"):
                return s
            time.sleep(0.1)
        self.fail(f"never connected: {s}")

    def test_imports_the_headset_and_connects_through_its_port(self):
        s = self.wait_connected()
        self.assertEqual(s["phase"], "connected", s["error"])
        self.assertEqual(s["via"]["host"], "localhost")
        self.assertEqual(s["device"]["alias"], "frame")
        self.assertEqual(s["device"]["name"], "Steam Frame")
        self.assertEqual(s["probes"][0]["host"], "localhost")
        status, devices = self.request("GET", "/api/devices")
        self.assertEqual(status, 200)
        self.assertEqual([d["alias"] for d in devices["devices"]], ["frame"])
        self.assertEqual(devices["nextAlias"], "frame-2")

    def test_events_stream_the_state(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request("GET", "/api/connection/events", headers={"X-Frame-UI": "1"})
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream")
        line = r.fp.readline()
        self.assertTrue(line.startswith(b"data: "), line)
        self.assertIn("stages", json.loads(line[6:]))
        conn.close()

    def test_guards_and_validation(self):
        self.assertEqual(self.request("GET", "/api/connection", key="")[0], 403)
        self.assertEqual(self.request("GET", "/api/devices", key="nope")[0], 403)
        self.assertEqual(self.request("POST", "/api/devices", {"action": "address-add", "id": "x", "host": "a;b"})[0], 400)
        self.assertEqual(self.request("POST", "/api/devices", {"action": "setup", "alias": "-oProxyCommand=x"})[0], 400)
        self.assertEqual(self.request("POST", "/api/devices", {"action": "nope"})[0], 400)


if __name__ == "__main__":
    unittest.main()
