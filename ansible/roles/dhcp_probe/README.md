# `dhcp_probe` role

Answers "does the production DHCP service answer the LAN?" from outside the cluster. A systemd
timer on `srviac` sends one DHCPDISCOVER to the DHCP LoadBalancer the way the router's relay does
— `giaddr` set to srviac's own address, sent from and answered on UDP 67 — and writes the result
to node-exporter's textfile collector, which the in-cluster Prometheus scrapes. It sends no
REQUEST, so dnsmasq keeps no lease and DHCPApp never sees the probe.

Applied by `site.yml`'s srviac play; `IaC/Apply` excludes that host, so the operator runs
`site.yml --limit srviac`.

## Metrics

`/var/lib/prometheus/node-exporter/dhcp_probe.prom`, every series labelled
`server="<dhcp_probe_server>"`:

- `dhcp_probe_success` — 1 when the last DISCOVER got an OFFER within `dhcp_probe_timeout`, else 0.
- `dhcp_probe_duration_seconds` — from the DISCOVER to the OFFER, or the timeout.
- `dhcp_probe_last_run_timestamp_seconds` — when the last probe finished. A probe that errors
  (UDP 67 taken, no route) writes nothing, so its result goes stale.

## Running it by hand

`sudo systemctl start dhcp-probe.service`, then `journalctl -u dhcp-probe` shows the OFFER and the
time it took; `dnsmasq-prd`'s `dhcp-dnsmasq` container logs `DHCPDISCOVER`/`DHCPOFFER` for
`02:00:00:40:04:01`.

## Tests

`python3 -m unittest discover -s ansible/roles/dhcp_probe/tests` (part of the root component's
`kc project test`) covers the packet and the metrics file. The exchange itself needs root and the
LAN, so it is witnessed on srviac.
