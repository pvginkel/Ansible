import os
import socket
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

import dhcp_probe  # noqa: E402

CHADDR = dhcp_probe.parse_mac("02:00:00:40:04:01")
XID = 0x12345678


def reply(xid=XID, chaddr=CHADDR, op=2, msgtype=dhcp_probe.DHCPOFFER, yiaddr="10.1.1.80"):
    """A server reply shaped like dnsmasq's OFFER witnessed on 2026-10-03."""
    packet = bytearray(dhcp_probe.build_discover(xid, "10.1.0.45", chaddr))
    packet[0] = op
    packet[16:20] = socket.inet_aton(yiaddr)
    options = bytes([53, 1, msgtype, 54, 4]) + socket.inet_aton("172.16.94.190") + bytes([255])
    return bytes(packet[:240]) + options


class BuildDiscoverTest(unittest.TestCase):
    def test_is_a_relayed_discover(self):
        packet = dhcp_probe.build_discover(XID, "10.1.0.45", CHADDR)
        self.assertGreaterEqual(len(packet), dhcp_probe.MIN_MESSAGE_LENGTH)
        op, htype, hlen, hops, xid = struct.unpack("!BBBBI", packet[:8])
        self.assertEqual((op, htype, hlen, hops, xid), (1, 1, 6, 1, XID))
        self.assertEqual(socket.inet_ntoa(packet[24:28]), "10.1.0.45")
        self.assertEqual(packet[28:34], CHADDR)
        self.assertEqual(packet[236:240], dhcp_probe.MAGIC_COOKIE)
        options = dhcp_probe.parse_options(packet[240:])
        self.assertEqual(options[53], bytes([dhcp_probe.DHCPDISCOVER]))
        self.assertIn(55, options)


class OfferedAddressTest(unittest.TestCase):
    def test_matching_offer(self):
        self.assertEqual(dhcp_probe.offered_address(reply(), XID, CHADDR), "10.1.1.80")

    def test_other_transaction(self):
        self.assertIsNone(dhcp_probe.offered_address(reply(xid=XID + 1), XID, CHADDR))

    def test_other_client(self):
        other = dhcp_probe.parse_mac("02:00:00:00:00:02")
        self.assertIsNone(dhcp_probe.offered_address(reply(chaddr=other), XID, CHADDR))

    def test_not_an_offer(self):
        self.assertIsNone(dhcp_probe.offered_address(reply(msgtype=6), XID, CHADDR))

    def test_own_request_echoed(self):
        self.assertIsNone(dhcp_probe.offered_address(reply(op=1), XID, CHADDR))

    def test_truncated(self):
        self.assertIsNone(dhcp_probe.offered_address(reply()[:200], XID, CHADDR))


class ParseMacTest(unittest.TestCase):
    def test_rejects_wrong_length(self):
        with self.assertRaises(ValueError):
            dhcp_probe.parse_mac("02:00:00:40:04")


class MetricsTest(unittest.TestCase):
    def test_offer(self):
        text = dhcp_probe.render_metrics("10.2.1.10", "10.1.1.80", 3.0184, 1759500000.4)
        self.assertIn('dhcp_probe_success{server="10.2.1.10"} 1\n', text)
        self.assertIn('dhcp_probe_duration_seconds{server="10.2.1.10"} 3.018\n', text)
        self.assertIn('dhcp_probe_last_run_timestamp_seconds{server="10.2.1.10"} 1759500000\n', text)

    def test_no_offer(self):
        text = dhcp_probe.render_metrics("10.2.1.10", None, 10.0, 1759500000)
        self.assertIn('dhcp_probe_success{server="10.2.1.10"} 0\n', text)

    def test_write_replaces_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            dhcp_probe.write_metrics(directory, "old\n")
            dhcp_probe.write_metrics(directory, "new\n")
            self.assertEqual(os.listdir(directory), [dhcp_probe.METRICS_FILE])
            with open(os.path.join(directory, dhcp_probe.METRICS_FILE), encoding="ascii") as f:
                self.assertEqual(f.read(), "new\n")


if __name__ == "__main__":
    unittest.main()
