"""Which network this computer is on, and whether Tailscale is up.

Frame Control remembers which of a headset's addresses worked on which network,
so it needs a stable name for "this network". The Wi-Fi name (SSID) is the
friendly one, but macOS 14+ hides it from apps without Location permission, and
wired networks have none. So every network is identified by a fingerprint of
its default gateway: the router's IP and MAC address, which stay the same for a
given home or office network. The user can give a fingerprint a name.

Runs on this computer (macOS, Linux, Windows). Python stdlib only; every probe
is a short command with a timeout, and each parser has fixtures in
tests/test_network.py.
"""
import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import time

import frame_host

TIMEOUT = 3


def run(argv, timeout=TIMEOUT):
    """A command's stdout, or "" if it's missing, fails or takes too long."""
    try:
        r = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout,
                           **({"creationflags": subprocess.CREATE_NO_WINDOW} if frame_host.WINDOWS else {}))
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


def valid_ip(text):
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def norm_mac(text):
    """"b4:fb:e4:1:87:3f" or "B4-FB-E4-01-87-3F" -> "b4:fb:e4:01:87:3f"; None if it isn't a MAC."""
    parts = re.split(r"[:-]", (text or "").strip())
    if len(parts) != 6 or not all(re.fullmatch(r"[0-9A-Fa-f]{1,2}", p) for p in parts):
        return None
    mac = ":".join(p.lower().zfill(2) for p in parts)
    return None if mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff") else mac


# ---- default gateway -------------------------------------------------------

def parse_route_macos(text):
    """`route -n get default` -> (gateway, interface)."""
    gw = re.search(r"^\s*gateway:\s*(\S+)", text, re.M)
    iface = re.search(r"^\s*interface:\s*(\S+)", text, re.M)
    gateway = gw.group(1) if gw and valid_ip(gw.group(1)) else None
    return gateway, iface.group(1) if iface else None


def parse_route_linux(text):
    """`ip -4 route show default` -> (gateway, interface) of the lowest-metric route."""
    best = None
    for line in text.splitlines():
        m = re.search(r"^default via (\S+) dev (\S+)", line.strip())
        if not m or not valid_ip(m.group(1)):
            continue
        metric = re.search(r"\bmetric (\d+)", line)
        key = int(metric.group(1)) if metric else 0
        if best is None or key < best[0]:
            best = (key, m.group(1), m.group(2))
    return (best[1], best[2]) if best else (None, None)


def parse_route_windows(text):
    """`route print -4 0.0.0.0` -> (gateway, local IP of the interface), lowest metric wins."""
    best = None
    for line in text.splitlines():
        f = line.split()
        if len(f) == 5 and f[0] == "0.0.0.0" and f[1] == "0.0.0.0" and valid_ip(f[2]) and f[4].isdigit():
            if best is None or int(f[4]) < best[0]:
                best = (int(f[4]), f[2], f[3])
    return (best[1], best[2]) if best else (None, None)


# ---- the gateway's MAC address ----------------------------------------------

def parse_arp_macos(text, ip):
    """`arp -n IP` -> MAC ("? (192.168.1.1) at b4:fb:e4:b5:67:55 on en0 ifscope [ethernet]")."""
    m = re.search(r"\(" + re.escape(ip) + r"\) at (\S+)", text)
    return norm_mac(m.group(1)) if m else None


def parse_neigh_linux(text, ip):
    """`ip neigh show IP` -> MAC ("192.168.1.1 dev wlan0 lladdr b4:fb:... REACHABLE")."""
    for line in text.splitlines():
        f = line.split()
        if f and f[0] == ip and "lladdr" in f:
            return norm_mac(f[f.index("lladdr") + 1]) if f.index("lladdr") + 1 < len(f) else None
    return None


def parse_arp_windows(text, ip):
    """`arp -a IP` -> MAC ("  192.168.1.1           b4-fb-e4-b5-67-55     dynamic")."""
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 2 and f[0] == ip:
            return norm_mac(f[1])
    return None


# ---- Wi-Fi name --------------------------------------------------------------

def parse_summary_macos(text):
    """`ipconfig getsummary IFACE` -> (ssid, is_wifi). macOS prints "<redacted>" without Location permission."""
    kind = re.search(r"^\s*InterfaceType\s*:\s*(\S+)", text, re.M)
    ssid = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", text, re.M)
    name = ssid.group(1) if ssid else None
    if name in ("<redacted>", ""):
        name = None
    return name, (kind.group(1).lower() == "wifi") if kind else None


def parse_nmcli(text):
    """`nmcli -t -f active,ssid dev wifi` -> the active SSID (colons in names come escaped as \\:)."""
    for line in text.splitlines():
        if line.startswith("yes:"):
            return line[4:].replace("\\:", ":") or None
    return None


def parse_netsh(text):
    """`netsh wlan show interfaces` -> the connected SSID (not the BSSID line)."""
    state = re.search(r"^\s*State\s*:\s*(\S+)", text, re.M)
    ssid = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", text, re.M)
    if not ssid or (state and state.group(1).lower() != "connected"):
        return None
    return ssid.group(1)


# ---- Tailscale -----------------------------------------------------------------

def tailscale_cli():
    extra = []
    if frame_host.MAC:
        extra.append("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
    elif frame_host.WINDOWS:
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                extra.append(os.path.join(base, "Tailscale", "tailscale.exe"))
    return frame_host.which("tailscale", *extra)


def parse_tailscale(text):
    """`tailscale status --json` -> {"up", "ip", "name", "tailnet", "peers": [...]}.

    Each peer: {"name", "dns" (MagicDNS name, no trailing dot), "ips", "os", "online"}.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return {"up": False, "peers": []}
    if not isinstance(data, dict):
        return {"up": False, "peers": []}
    me = data.get("Self") or {}
    tailnet = (data.get("CurrentTailnet") or {}).get("Name") if isinstance(data.get("CurrentTailnet"), dict) else None
    out = {"up": data.get("BackendState") == "Running",
           "ip": next((ip for ip in me.get("TailscaleIPs") or [] if "." in ip), None),
           "name": (me.get("DNSName") or "").rstrip(".") or None,
           "tailnet": tailnet, "peers": []}
    for p in (data.get("Peer") or {}).values():
        if not isinstance(p, dict):
            continue
        out["peers"].append({"name": p.get("HostName") or "", "dns": (p.get("DNSName") or "").rstrip("."),
                             "ips": [ip for ip in p.get("TailscaleIPs") or [] if isinstance(ip, str)],
                             "os": p.get("OS") or "", "online": bool(p.get("Online"))})
    return out


def tailscale_status():
    cli = tailscale_cli()
    if not cli:
        return {"up": False, "installed": False, "peers": []}
    out = parse_tailscale(run([cli, "status", "--json"], timeout=4) or "{}")
    out["installed"] = True
    return out


TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def is_tailscale(host):
    host = host.lower().rstrip(".")
    if host.endswith(".ts.net"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip in (TAILNET_V4 if ip.version == 4 else TAILNET_V6)


def guess_kind(host):
    """What sort of address a host is: mdns, tailscale, lan or manual."""
    h = host.lower().rstrip(".")
    if h.endswith(".local"):
        return "mdns"
    if is_tailscale(h):
        return "tailscale"
    try:
        ip = ipaddress.ip_address(h.split("%")[0])
        if ip.is_private or ip.is_link_local:
            return "lan"
    except ValueError:
        pass
    return "manual"


# ---- putting it together ----------------------------------------------------------

def network_id(gateway, mac):
    """A short, stable id for a network: its gateway's IP and MAC. None until both are known."""
    if not gateway or not mac:
        return None
    return "n-" + hashlib.sha1(f"{gateway}|{mac}".encode()).hexdigest()[:10]


def local_ip(towards="192.0.2.1"):
    """This computer's address on the default route (UDP connect sends nothing)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((towards, 9))
            return s.getsockname()[0]
    except OSError:
        return None


def gateway():
    """(gateway IP, interface) of the default route."""
    if frame_host.MAC:
        return parse_route_macos(run(["route", "-n", "get", "default"]))
    if frame_host.WINDOWS:
        return parse_route_windows(run(["route", "print", "-4", "0.0.0.0"]))
    return parse_route_linux(run(["ip", "-4", "route", "show", "default"]))


def gateway_mac(ip):
    if frame_host.MAC:
        return parse_arp_macos(run(["arp", "-n", ip]), ip)
    if frame_host.WINDOWS:
        return parse_arp_windows(run(["arp", "-a", ip]), ip)
    return parse_neigh_linux(run(["ip", "neigh", "show", ip]), ip)


def poke(ip):
    """Make the system look up the gateway's MAC (an ARP entry can expire)."""
    try:
        with socket.create_connection((ip, 53), timeout=0.3):
            pass
    except OSError:
        pass


def wifi(interface):
    """(ssid or None, is_wifi or None) for the default route's interface."""
    if frame_host.MAC:
        if interface:
            ssid, is_wifi = parse_summary_macos(run(["ipconfig", "getsummary", interface]))
            if ssid or is_wifi is False:
                return ssid, is_wifi
            m = re.search(r"Current Wi-Fi Network: (.+)", run(["networksetup", "-getairportnetwork", interface]))
            return (m.group(1).strip() if m else None), is_wifi
        return None, None
    if frame_host.WINDOWS:
        ssid = parse_netsh(run(["netsh", "wlan", "show", "interfaces"]))
        return ssid, True if ssid else None
    if frame_host.which("nmcli"):
        ssid = parse_nmcli(run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]))
    else:
        ssid = run(["iwgetid", "-r"]).strip() or None
    return ssid, True if ssid else (interface.startswith(("wl", "wlan")) if interface else None)


def fingerprint():
    """The cheap part, polled every few seconds: (gateway, interface, gateway MAC)."""
    gw, iface = gateway()
    mac = None
    if gw:
        mac = gateway_mac(gw)
        if not mac:
            poke(gw)
            mac = gateway_mac(gw)
    return gw, iface, mac


def current_network(fp=None, with_tailscale=True):
    """Everything the connection status shows about this computer's network."""
    gw, iface, mac = fp or fingerprint()
    ssid, is_wifi = wifi(iface) if gw else (None, None)
    net = {"id": network_id(gw, mac), "gateway": gw, "gateway_mac": mac, "interface": iface,
           "ssid": ssid, "wifi": is_wifi, "local_ip": local_ip(gw) if gw else None, "checked": time.time()}
    if with_tailscale:
        ts = tailscale_status()
        net["tailscale"] = {k: ts.get(k) for k in ("up", "installed", "ip", "name", "tailnet")}
    return net
