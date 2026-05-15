# Ceph dashboard VIP runbook

Manual procedure for the `ceph.home` virtual IP (`10.1.0.38`) in front
of the three-node microceph cluster — `srvceph1` / `srvceph2` /
`srvceph3`.

Ceph's mgr redirects dashboard traffic to whichever node is currently
mgr-active, and the redirect lands on that node's *backplane* address
(`192.168.188.0/24`) — unreachable from a workstation. A
leader-tracking Keepalived VIP pins `ceph.home` to the active mgr's
LAN address, so dashboard traffic lands on the right node directly
with no redirect.

**This is v1, and it is manual.** srvceph1/2/3 are not Ansible-managed
yet — Phase 5 brings them in. Until then the operator applies this
config by hand. Phase 5's Ceph role picks up exactly the shape below
via the reusable `keepalived` role (`ansible/roles/keepalived/`), so
keep any local change in sync with this runbook.

Design context:
[`/work/AnsibleSpecs/slices/internal-ha-vips.md`](../../../AnsibleSpecs/slices/internal-ha-vips.md)
§C.

## Conventions

- All three nodes sit on the same LAN bridge — VRRP requires Layer-2
  adjacency, and this is what provides it.
- The VIP rides `ens18`, the LAN-facing device.
- "Roboform" is the operator's password manager of record.

## What gets installed

Identical on all three nodes except the three per-node values in the
table further down:

| Item | Path | Mode |
|---|---|---|
| Track script | `/etc/keepalived/scripts/ceph-mgr-active.sh` | `0755 root:root` |
| Keepalived config | `/etc/keepalived/keepalived.conf` | `0644 root:root` |

### Track script — `ceph-mgr-active.sh`

Exits `0` only on the node that is currently the active Ceph mgr.
Keepalived adds `weight 50` to that node's priority, pulling the VIP
to it.

```sh
#!/bin/sh
# Exit 0 if this node is the active Ceph mgr, non-zero otherwise.
# Keepalived adds `weight` to the priority when this exits 0.
active=$(microceph.ceph mgr stat --format json 2>/dev/null | jq -r .active_name)
[ "$active" = "$(hostname -s)" ]
```

### Keepalived config — `keepalived.conf`

```conf
# /etc/keepalived/keepalived.conf
global_defs {
    router_id <ROUTER_ID>             # per-node — see table
    enable_script_security
}

vrrp_script chk_ceph_mgr {
    script "/etc/keepalived/scripts/ceph-mgr-active.sh"
    interval 2
    timeout 2
    fall 2
    rise 1
    weight 50                         # +50 priority bump while this node is mgr-active
}

vrrp_instance VI_ceph {
    state BACKUP
    interface ens18
    virtual_router_id 52
    priority 100                      # same on all three; the script differential decides
    advert_int 1

    unicast_src_ip <SRC_IP>           # per-node — see table
    unicast_peer {
        <PEER_IP_1>                   # per-node — see table
        <PEER_IP_2>
    }

    authentication {
        auth_type PASS
        auth_pass <SHARED_VRRP_SECRET>
    }

    virtual_ipaddress {
        10.1.0.38/16 dev ens18
    }

    track_script {
        chk_ceph_mgr
    }
}
```

`<SHARED_VRRP_SECRET>` is the homelab-wide VRRP auth string (max 8
characters) — the same value the k8s API and OpenBao VIPs use. It is
in Roboform; in the Ansible repo it is the ansible-vault'd
`vrrp_auth_password` in `ansible/inventories/prd/group_vars/all/vips.yml`.

### Per-node values

| Node | `<ROUTER_ID>` | `<SRC_IP>` | `<PEER_IP_1>` | `<PEER_IP_2>` |
|---|---|---|---|---|
| srvceph1 | `srvceph1` | `10.1.0.24` | `10.1.0.25` | `10.1.0.26` |
| srvceph2 | `srvceph2` | `10.1.0.25` | `10.1.0.24` | `10.1.0.26` |
| srvceph3 | `srvceph3` | `10.1.0.26` | `10.1.0.24` | `10.1.0.25` |

## How the leader-tracking works

All three nodes carry base `priority 100`. The track script succeeds
only on the mgr-active node, which adds `weight 50` → effective
`150` → that node wins the election and holds the VIP. When Ceph
re-elects a new mgr, the old node's script starts failing (priority
falls back to `100`), the new active node's script starts succeeding
(climbs to `150`), and the VIP moves. With `interval 2` / `fall 2`,
failover takes ~2–6 s.

## Rollout

Per node, one at a time. Save the script and config locally first
(substituting the per-node values), then:

```sh
sudo apt install -y keepalived jq
sudo install -d -m 0755 /etc/keepalived/scripts
sudo install -m 0755 ceph-mgr-active.sh /etc/keepalived/scripts/
sudo install -m 0644 keepalived.conf /etc/keepalived/
sudo systemctl enable --now keepalived
```

`jq` and `microceph.ceph` must both be on `PATH` for `root` —
keepalived runs the track script as `root`. `enable_script_security`
only enforces that the script's path is not writable by a non-root
user, which `0755 root:root` under `/etc/keepalived/scripts/`
satisfies — so no `script_user` line is needed.

## Verification

After all three nodes are up:

- `ping ceph.home` resolves to `10.1.0.38` and replies.
- `ip -4 addr show dev ens18` shows `10.1.0.38` on **exactly one**
  node — the current mgr-active one. Confirm which that is with
  `microceph.ceph mgr stat`.
- `curl -kI https://ceph.home:8443/` returns the dashboard login page.
- Force a mgr re-election with `microceph.ceph mgr fail <active>`; the
  VIP migrates to the newly-active node within ~5 s and `ip addr`
  confirms it moved.
- Kill the mgr on the VIP-holding node; the track script fails on its
  next poll, Keepalived demotes the node's priority, and the VIP moves
  to whichever node Ceph elects active next.

## DNS

`ceph.home → 10.1.0.38` is a dnsmasq static host entry in HelmCharts
`configs/prd/dnsmasq.yaml` (`name: ceph`). No change needed here as
long as that entry is present.

## Phase 5 handoff

When Phase 5 brings srvceph1/2/3 under Ansible, the Ceph role includes
the `keepalived` role with a `keepalived_track_script` carrying the
`ceph-mgr-active.sh` body above, `keepalived_virtual_router_id: 52`,
and the VIP from `group_vars/all/vips.yml`. The rendered
`keepalived.conf` must match this runbook byte-for-byte (modulo
the `# /etc/keepalived/keepalived.conf` header comment); if it
diverges, reconcile here first.
