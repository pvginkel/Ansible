# `microk8s` role

Installs and configures microk8s on a k8s node. Lands an Ubuntu host at "ready to host workloads" — kernel modules, Ceph CSI client tooling, the snap pinned to a channel, the launch-config (`.microk8s.yaml`) with cluster CIDRs and extraSANs, the Calico interface autodetect patch, and the configured addon set. OS users get added to the `microk8s` group and `kubectl` is aliased system-wide.

## Scope

This role covers single-node bring-up. Multi-node join logic and capability-label reconciliation arrive in subsequent commits (Phase 4 step 3 / step 4 respectively); MetalLB IPAddressPool reconciliation is a follow-up too. The set of moving parts handled today:

| Concern | Mechanism |
|---|---|
| Kernel modules | `/etc/modules-load.d/<module>.conf` + `modprobe` for the running boot |
| Ceph client | `apt` install of `ceph-common`, `rbd-nbd` |
| `.microk8s.yaml` | `template` from `launch-config.yaml.j2`, written before the snap install so first-init reads it |
| Snap install | `community.general.snap` at the pinned channel |
| Calico autodetect | `replace` on `cni.yaml` + handler that re-applies the daemonset |
| Addons | parse `microk8s status --format yaml`, enable any from `microk8s_addons` that are missing |
| Users | `user` module appends membership to the `microk8s` group; `~/.kube` per user |
| `kubectl` alias | `snap alias microk8s.kubectl kubectl`, guarded by a stat |

## Inventory contract

The role asserts that the four cluster-CIDR variables are set; per-cluster `group_vars/k8s_<cluster>.yml` is the right place. Each cluster (prd, dev, scratch) carries its own values — the defaults in `defaults/main.yml` are deliberately empty so a missing config fails loud rather than installing onto the wrong subnet.

| Variable | Per-cluster | Default | Notes |
|---|---|---|---|
| `microk8s_channel` | optional | `1.32/stable` | Pinned per `decisions.md` "k8s version policy." |
| `microk8s_calico_iface` | optional | `ens18` | Override only if a host class uses something else. |
| `microk8s_cluster_cidr_v4` | **required** | `""` | E.g. `172.16.0.0/16` (prd) or `172.20.0.0/16` (scratch). |
| `microk8s_service_cidr_v4` | **required** | `""` | E.g. `172.17.0.0/16` (prd). |
| `microk8s_cluster_cidr_v6` | **required** | `""` | IPv6 cluster CIDR (`/64`). |
| `microk8s_service_cidr_v6` | **required** | `""` | IPv6 service CIDR (`/108`). |
| `microk8s_extra_sans` | optional | `[]` | API-server cert SANs, typically the service gateway IP. |
| `microk8s_addons` | optional | `[dns, helm, helm3, metrics-server]` | Order matters; `community` must precede community-namespaced addons. |
| `microk8s_users` | optional | `[]` | OS users added to the `microk8s` group. |

## Idempotency notes

- The launch-config template renders to the same content on every run; subsequent applies are no-ops once it's in place. (Recall it only matters at first init — editing it post-install changes nothing in the cluster.)
- The Calico patch uses `replace` on `"first-found"` → `"interface=<iface>"`. After the first run the pattern is gone, so the task is a no-op and the handler doesn't fire.
- Addons enable only when missing from `microk8s status --format yaml`'s enabled list. Re-running the role against an already-converged node reports `changed=0`.
- The `kubectl` alias is guarded by a `stat` so `snap alias` only runs when `/snap/bin/kubectl` is absent.

## What this role doesn't do (yet)

- **MetalLB pool reconciliation.** The `metallb` addon (when in `microk8s_addons`) is enabled, but the IPAddressPool / L2Advertisement specs are not yet reconciled by this role. Adds in a subsequent commit via `kubernetes.core.k8s`.
- **Capability labels.** `homelab.local/storage`, `homelab.local/performance` are operator intent published from `host_vars`; reconciliation lands when the role gets the labels task.
- **Idempotent join.** This role currently bootstraps a node as if it stands alone; a second node added to the same cluster would not auto-join. Phase 4 step 3 wires the join path.
