#!/usr/bin/env python3
"""Send one relay-style DHCPDISCOVER and record whether an OFFER came back.

The result lands in a node-exporter textfile-collector file. The probe sends
no REQUEST, so the server keeps no lease for it.
"""

import argparse
import os
import secrets
import socket
import struct
import time

BOOTP_PORT = 67
MAGIC_COOKIE = b"\x63\x82\x53\x63"
DHCPDISCOVER = 1
DHCPOFFER = 2
# Subnet mask, router, DNS servers, lease time.
PARAMETER_REQUEST_LIST = bytes([1, 3, 6, 51])
# RFC 1542 §3.2: a BOOTP message is at least 300 octets.
MIN_MESSAGE_LENGTH = 300
METRICS_FILE = "dhcp_probe.prom"


def parse_mac(text):
    mac = bytes.fromhex(text.replace(":", ""))
    if len(mac) != 6:
        raise ValueError(f"not a MAC address: {text}")
    return mac


def build_discover(xid, giaddr, chaddr):
    header = struct.pack(
        "!BBBBIHH4s4s4s4s16s64s128s",
        1,  # op: BOOTREQUEST
        1,  # htype: Ethernet
        6,  # hlen
        1,  # hops: one relay, as the router's relay sends it
        xid,
        0,  # secs
        0,  # flags
        bytes(4),  # ciaddr
        bytes(4),  # yiaddr
        bytes(4),  # siaddr
        socket.inet_aton(giaddr),
        chaddr.ljust(16, b"\0"),
        bytes(64),  # sname
        bytes(128),  # file
    )
    options = (
        bytes([53, 1, DHCPDISCOVER])
        + bytes([55, len(PARAMETER_REQUEST_LIST)])
        + PARAMETER_REQUEST_LIST
        + bytes([255])
    )
    return (header + MAGIC_COOKIE + options).ljust(MIN_MESSAGE_LENGTH, b"\0")


def parse_options(data):
    options = {}
    i = 0
    while i < len(data):
        code = data[i]
        if code == 0:
            i += 1
            continue
        if code == 255 or i + 1 >= len(data):
            break
        length = data[i + 1]
        options[code] = data[i + 2 : i + 2 + length]
        i += 2 + length
    return options


def offered_address(packet, xid, chaddr):
    """Return the yiaddr of an OFFER answering (xid, chaddr), else None."""
    if len(packet) < 240 or packet[0] != 2:
        return None
    if struct.unpack("!I", packet[4:8])[0] != xid:
        return None
    if packet[28:34] != chaddr or packet[236:240] != MAGIC_COOKIE:
        return None
    if parse_options(packet[240:]).get(53) != bytes([DHCPOFFER]):
        return None
    return socket.inet_ntoa(packet[16:20])


def probe(server, giaddr, chaddr, timeout):
    """Return (offered address or None, seconds waited)."""
    xid = secrets.randbits(32)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # The server sends a relayed request's reply to giaddr on port 67,
        # from whichever address its outgoing NAT picks — not from the
        # service address. So the socket stays unconnected, listens on 67,
        # and matches replies on xid and chaddr alone.
        sock.bind(("0.0.0.0", BOOTP_PORT))
        start = time.monotonic()
        sock.sendto(build_discover(xid, giaddr, chaddr), (server, BOOTP_PORT))
        while True:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                return None, timeout
            sock.settimeout(remaining)
            try:
                packet, _ = sock.recvfrom(4096)
            except TimeoutError:
                return None, timeout
            address = offered_address(packet, xid, chaddr)
            if address is not None:
                return address, time.monotonic() - start


def render_metrics(server, offered, duration, finished):
    labels = f'{{server="{server}"}}'
    return (
        "# HELP dhcp_probe_success Whether the last relay-style DHCPDISCOVER got an OFFER.\n"
        "# TYPE dhcp_probe_success gauge\n"
        f"dhcp_probe_success{labels} {1 if offered else 0}\n"
        "# HELP dhcp_probe_duration_seconds Seconds from the DISCOVER to the OFFER, or to the timeout.\n"
        "# TYPE dhcp_probe_duration_seconds gauge\n"
        f"dhcp_probe_duration_seconds{labels} {duration:.3f}\n"
        "# HELP dhcp_probe_last_run_timestamp_seconds Unix time at which the last probe finished.\n"
        "# TYPE dhcp_probe_last_run_timestamp_seconds gauge\n"
        f"dhcp_probe_last_run_timestamp_seconds{labels} {finished:.0f}\n"
    )


def write_metrics(directory, text):
    # node-exporter reads only *.prom files, so the temporary name is never
    # collected half-written; the rename is atomic within the directory.
    path = os.path.join(directory, METRICS_FILE)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="ascii") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="DHCP server address")
    parser.add_argument("--giaddr", required=True, help="this host's address, as the relay agent address")
    parser.add_argument("--chaddr", required=True, help="client hardware address to ask for")
    parser.add_argument("--timeout", type=float, required=True, help="seconds to wait for the OFFER")
    parser.add_argument("--textfile-dir", required=True, help="node-exporter textfile-collector directory")
    args = parser.parse_args()

    offered, duration = probe(args.server, args.giaddr, parse_mac(args.chaddr), args.timeout)
    write_metrics(args.textfile_dir, render_metrics(args.server, offered, duration, time.time()))
    print(f"OFFER {offered} after {duration:.3f}s" if offered else f"no OFFER within {args.timeout}s")


if __name__ == "__main__":
    main()
