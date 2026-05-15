# `keepalived` role

Renders one Keepalived VRRP instance on a host so a cluster can be
reached by a stable virtual IP (VIP) instead of a specific node's
address. Reusable and host-class-agnostic: callers `include_role` it
with per-call vars, the same way the `microk8s` role wires up the
kube-apiserver VIP.

Design context: [`/work/AnsibleSpecs/slices/internal-ha-vips.md`](../../../../AnsibleSpecs/slices/internal-ha-vips.md).

## Mental model

VRRP elects exactly one *master* among the peers sharing a
`virtual_router_id`; the master holds the VIP. The role writes one
node's side of that election. Run it on every member of the cluster
with the same `virtual_router_id` + `keepalived_password` and a VIP
appears on whichever member currently wins.

Two flavours, selected by whether `keepalived_track_script` is set:

- **Plain VRRP** — the highest static `keepalived_priority` wins. Used
  where any node is equally able to serve (e.g. the microk8s API,
  served from every control-plane node).
- **Leader-tracking VRRP** — every node carries the same base
  priority; a `vrrp_script` polls a leader-detection check and adds
  `weight` to the node that currently leads, pulling the VIP to it.
  Used where only one node should receive traffic (a Ceph mgr-active
  node, an OpenBao Raft leader).

## Inputs

Required (the role asserts these):

- `keepalived_vip` — the VIP address.
- `keepalived_virtual_router_id` — VRRP virtual-router-id, `1`–`255`.
  **Must be unique across every VRRP group on the LAN** — the homelab
  allocation lives in `inventories/prd/group_vars/all/vips.yml`.
- `keepalived_password` — shared VRRPv2 auth string, **1–8 characters**
  (VRRPv2 truncates silently past 8). Supply it ansible-vault'd.

Optional (defaults in [`defaults/main.yml`](defaults/main.yml)):

- `keepalived_interface` — interface the VIP binds to and adverts ride.
  Defaults to the default-route NIC (the LAN device — `ens18` on
  Terraform-provisioned VMs).
- `keepalived_vip_prefix` — prefix length the VIP is bound with.
  Defaults to the node's own LAN prefix.
- `keepalived_priority` — base VRRP priority (default `100`).
- `keepalived_instance_name` — VRRP instance name (default
  `VI_<router_id>`).
- `keepalived_advert_int` — advertisement interval, seconds (default `1`).
- `keepalived_unicast_peers` / `keepalived_unicast_src_ip` — when
  `keepalived_unicast_peers` is non-empty the instance uses unicast
  VRRP to those peer IPs (preferred on the homelab LAN — multicast
  across the bridges is unvalidated). `src_ip` defaults to this node's
  LAN address. Empty peers = multicast.
- `keepalived_track_script` — leader-tracking script. A dict of
  `{name, content, interval, timeout, fall, rise, weight}`; see the
  defaults file for what each key does. When set, the role installs
  `<name>.sh` under `keepalived_scripts_dir` (`0755 root:root`) and
  renders the matching `vrrp_script` + `track_script` blocks.

## What it does

1. `apt install keepalived`.
2. When `keepalived_track_script` is set, install the script under
   `keepalived_scripts_dir`.
3. Render `/etc/keepalived/keepalived.conf` from the inputs.
4. Enable `keepalived.service`; restart it on a config or script change.

Every step is idempotent — a converged host reports `ok` throughout.

## Exercising the role

The role has no playbook of its own. To smoke-test it standalone (e.g.
against a scratch VM before wiring a real consumer), include it from an
ad-hoc play — plain VRRP:

```yaml
- hosts: wrkscratch
  become: true
  tasks:
    - ansible.builtin.include_role:
        name: keepalived
      vars:
        keepalived_vip: 10.1.0.250
        keepalived_virtual_router_id: 99
        keepalived_password: scratch01
```

and leader-tracking, to exercise the `vrrp_script` path:

```yaml
        keepalived_track_script:
          name: chk_demo
          content: |
            #!/bin/sh
            exit 0
          interval: 2
          timeout: 2
          fall: 2
          rise: 1
          weight: 50
```

`ip -4 addr show` on the host then shows the VIP as a secondary
address once the node wins the election.

## Constraints

- **VRRP requires Layer-2 adjacency** between all peers in a group.
- **VRRPv2 only** — `auth_pass` is limited to 8 characters and the
  shared secret crosses the wire in cleartext. Treat the VIP fabric as
  LAN-internal.
- **One VRRP instance per host.** The role owns the whole of
  `keepalived.conf`, so a second inclusion overwrites the first. No
  homelab host fronts two VIPs today; multi-instance support would
  need the render to merge instances rather than replace the file.
