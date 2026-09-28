"""frame_network's parsers, with what macOS, Linux and Windows print.

Run: python3 -m unittest discover -s tests
"""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import frame_network as fn  # noqa: E402

MAC_ROUTE = """   route to: default
destination: default
       mask: default
    gateway: 192.168.1.1
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
"""
MAC_ARP = "? (192.168.1.1) at b4:fb:e4:1:87:3f on en0 ifscope [ethernet]\n"
MAC_ARP_INCOMPLETE = "? (192.168.1.1) at (incomplete) on en0 ifscope [ethernet]\n"
MAC_SUMMARY = """<dictionary> {
  BSSID : <redacted>
  ConnectionID : 1
  InterfaceType : WiFi
  LinkStatusActive : TRUE
  NetworkID : <redacted>
  SSID : <redacted>
  Security : WPA2_PSK
}"""
MAC_SUMMARY_NAMED = MAC_SUMMARY.replace("SSID : <redacted>\n  Security", "SSID : Home Net\n  Security")
MAC_SUMMARY_WIRED = "<dictionary> {\n  InterfaceType : Ethernet\n  LinkStatusActive : TRUE\n}"

LINUX_ROUTE = """default via 10.0.0.1 dev wlp2s0 proto dhcp src 10.0.0.23 metric 600
default via 192.168.50.1 dev enp3s0 proto dhcp src 192.168.50.9 metric 100
"""
LINUX_NEIGH = "192.168.50.1 dev enp3s0 lladdr 00:11:22:aa:bb:cc REACHABLE\n"
NMCLI = "no:Neighbour\nyes:Cafe\\: upstairs\nno:\n"

WIN_ROUTE = """===========================================================================
Interface List
 12...00 15 5d 01 02 03 ......Intel(R) Wi-Fi 6 AX201 160MHz
===========================================================================

IPv4 Route Table
===========================================================================
Active Routes:
Network Destination        Netmask          Gateway       Interface  Metric
          0.0.0.0          0.0.0.0     192.168.0.254    192.168.0.40     50
          0.0.0.0          0.0.0.0      192.168.1.1    192.168.1.50     35
===========================================================================
Persistent Routes:
  None
"""
WIN_ARP = """
Interface: 192.168.1.50 --- 0xc
  Internet Address      Physical Address      Type
  192.168.1.1           b4-fb-e4-b5-67-55     dynamic
"""
NETSH = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    State                  : connected
    SSID                   : Office 5G
    BSSID                  : 12:34:56:78:9a:bc
    Network type           : Infrastructure
"""
NETSH_OFF = NETSH.replace("State                  : connected", "State                  : disconnected")

TAILSCALE = json.dumps({
    "BackendState": "Running",
    "CurrentTailnet": {"Name": "example.github"},
    "Self": {"HostName": "laptop", "DNSName": "laptop.tail1234.ts.net.", "TailscaleIPs": ["fd7a:115c:a1e0::1", "100.101.102.103"]},
    "Peer": {"nodekey:1": {"HostName": "frame", "DNSName": "frame.tail1234.ts.net.", "OS": "linux", "Online": True,
                           "TailscaleIPs": ["100.113.174.84", "fd7a:115c:a1e0::5928:ae55"]},
             "nodekey:2": {"HostName": "phone", "DNSName": "phone.tail1234.ts.net.", "OS": "iOS", "Online": False,
                           "TailscaleIPs": ["100.77.1.2"]}},
})


class Parsers(unittest.TestCase):
    def test_macos(self):
        self.assertEqual(fn.parse_route_macos(MAC_ROUTE), ("192.168.1.1", "en0"))
        self.assertEqual(fn.parse_route_macos("route: writing to routing socket: not in table\n"), (None, None))
        self.assertEqual(fn.parse_arp_macos(MAC_ARP, "192.168.1.1"), "b4:fb:e4:01:87:3f")  # padded
        self.assertIsNone(fn.parse_arp_macos(MAC_ARP_INCOMPLETE, "192.168.1.1"))
        self.assertIsNone(fn.parse_arp_macos(MAC_ARP, "192.168.1.10"))
        self.assertEqual(fn.parse_summary_macos(MAC_SUMMARY), (None, True))  # no Location permission
        self.assertEqual(fn.parse_summary_macos(MAC_SUMMARY_NAMED), ("Home Net", True))
        self.assertEqual(fn.parse_summary_macos(MAC_SUMMARY_WIRED), (None, False))

    def test_linux(self):
        self.assertEqual(fn.parse_route_linux(LINUX_ROUTE), ("192.168.50.1", "enp3s0"))  # lowest metric
        self.assertEqual(fn.parse_route_linux(""), (None, None))
        self.assertEqual(fn.parse_neigh_linux(LINUX_NEIGH, "192.168.50.1"), "00:11:22:aa:bb:cc")
        self.assertIsNone(fn.parse_neigh_linux("192.168.50.1 dev enp3s0 FAILED\n", "192.168.50.1"))
        self.assertEqual(fn.parse_nmcli(NMCLI), "Cafe: upstairs")
        self.assertIsNone(fn.parse_nmcli("no:Neighbour\n"))

    def test_windows(self):
        self.assertEqual(fn.parse_route_windows(WIN_ROUTE), ("192.168.1.1", "192.168.1.50"))
        self.assertEqual(fn.parse_arp_windows(WIN_ARP, "192.168.1.1"), "b4:fb:e4:b5:67:55")
        self.assertEqual(fn.parse_netsh(NETSH), "Office 5G")  # not the BSSID
        self.assertIsNone(fn.parse_netsh(NETSH_OFF))

    def test_mac_addresses(self):
        self.assertEqual(fn.norm_mac("B4-FB-E4-B5-67-55"), "b4:fb:e4:b5:67:55")
        for bad in ("", "(incomplete)", "ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00", "b4:fb:e4:b5:67", "zz:fb:e4:b5:67:55"):
            self.assertIsNone(fn.norm_mac(bad), bad)

    def test_network_id_is_stable_and_needs_both_parts(self):
        a = fn.network_id("192.168.1.1", "b4:fb:e4:b5:67:55")
        self.assertEqual(a, fn.network_id("192.168.1.1", "b4:fb:e4:b5:67:55"))
        self.assertTrue(a.startswith("n-"))
        self.assertNotEqual(a, fn.network_id("192.168.1.1", "b4:fb:e4:b5:67:56"))  # same IP, another router
        self.assertIsNone(fn.network_id("192.168.1.1", None))
        self.assertIsNone(fn.network_id(None, "b4:fb:e4:b5:67:55"))

    def test_tailscale(self):
        ts = fn.parse_tailscale(TAILSCALE)
        self.assertTrue(ts["up"])
        self.assertEqual(ts["ip"], "100.101.102.103")
        self.assertEqual(ts["name"], "laptop.tail1234.ts.net")
        self.assertEqual(ts["tailnet"], "example.github")
        frame = ts["peers"][0]
        self.assertEqual((frame["name"], frame["dns"], frame["os"], frame["online"]),
                         ("frame", "frame.tail1234.ts.net", "linux", True))
        self.assertFalse(fn.parse_tailscale(json.dumps({"BackendState": "Stopped", "Self": {}}))["up"])
        self.assertEqual(fn.parse_tailscale("not json"), {"up": False, "peers": []})
        self.assertEqual(fn.parse_tailscale("[]"), {"up": False, "peers": []})

    def test_address_kinds(self):
        cases = {"frame.local": "mdns", "frame.local.": "mdns", "frame.tail1234.ts.net": "tailscale",
                 "100.113.174.84": "tailscale", "fd7a:115c:a1e0::5928:ae55": "tailscale",
                 "192.168.1.40": "lan", "10.0.0.5": "lan", "fe80::1%en0": "lan",
                 "frame.example.com": "manual", "8.8.8.8": "manual"}
        for host, kind in cases.items():
            self.assertEqual(fn.guess_kind(host), kind, host)


if __name__ == "__main__":
    unittest.main()
