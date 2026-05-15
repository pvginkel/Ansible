# `microk8s` role

Installs and configures microk8s on a k8s node. Lands an Ubuntu host at "ready to host workloads" — kernel modules, Ceph CSI client tooling, the snap pinned to a channel, the launch-config (`.microk8s.yaml`) with cluster CIDRs and extraSANs, the Calico interface autodetect patch, and the configured addon set. OS users get added to the `microk8s` group and `kubectl` is aliased system-wide.

## Scope

This role covers install, idempotent multi-node join, capability-label reconciliation, MetalLB pool reconciliation, registry-mirror config, node-local `/etc/hosts` entries, and full-Corefile CoreDNS reconciliation. The set of moving parts handled today:

| Concern | Mechanism |
|---|---|
| Kernel modules | `/etc/modules-load.d/<module>.conf` + `modprobe` for the running boot |
| Ceph client | `apt` install of `ceph-common`, `rbd-nbd` |
| `.microk8s.yaml` | `template` from `launch-config.yaml.j2`, written before the snap install so first-init reads it |
| Snap install | `community.general.snap` at the pinned channel |
| Calico autodetect | `replace` on `cni.yaml` + handler that re-applies the daemonset |
| Primary election | per-cluster runtime election from `microk8s status` across the inventory's `k8s_<env>` group; sets `microk8s_primary_host` as a fact (see `tasks/elect-primary.yml`) |
| Cluster join | `microk8s add-node` on the primary via `delegate_to` → `microk8s join` on the secondary, gated by an idempotency check on `high-availability.nodes` |
| Addons (primary only) | parse `microk8s status --format yaml`, enable any from `microk8s_addons` that are missing; the `metallb` addon's required IP-range arg is supplied inline as a sentinel and replaced by the role's MetalLB reconcile |
| Capability labels (primary only) | strategic-merge patch via `kubernetes.core.k8s` of each cluster member's Node object, sourcing labels from per-host `k8s_node_labels` |
| MetalLB pool (primary only) | `kubernetes.core.k8s` upserts the `IPAddressPool` and `L2Advertisement` the `metallb` addon creates, replacing the addon's colon-arg range with `microk8s_metallb_pool_addresses` |
| Registry mirrors | renders `/var/snap/microk8s/current/args/certs.d/<host>/hosts.toml` per `microk8s_registry_mirrors` entry; notifies a `microk8s stop && start` handler |
| Node-local `/etc/hosts` | blockinfile-managed entries from `microk8s_etc_hosts_entries` (containerd resolves image-pull names via node DNS, never via CoreDNS) |
| CoreDNS Corefile (primary only) | full-Corefile authoritative render via `kubernetes.core.k8s`, driven by `microk8s_coredns_hosts` and `microk8s_coredns_templates`; notifies a `kubectl rollout restart deployment/coredns` handler |
| Users | `user` module appends membership to the `microk8s` group; `~/.kube` per user |
| `kubectl` alias | `snap alias microk8s.kubectl kubectl`, guarded by a stat |
| kube-apiserver VIP | `include_role: keepalived` rendering a plain-VRRP instance for `kubernetes-api.home`; per-node, gated on `microk8s_manage_apiserver_vip` (see `tasks/keepalived.yml`) |

## Primary vs. secondary nodes

`microk8s_primary_host` is **elected at runtime** per cluster — see [`tasks/elect-primary.yml`](tasks/elect-primary.yml). The election picks (in priority): the lowest-hostname node already in a working cluster, then the lowest-hostname running-solo node, then the alphabetical first node in the `k8s_<env>` group as a greenfield seed. This means rebuilding the labeled-primary doesn't strand the surviving cluster members — the next role apply elects one of them.

Cluster boundary is the inventory `k8s_<env>` child group each host belongs to. Under `inventories/prd`, `hosts: k8s` resolves to `k8s_prd` and `k8s_dev` simultaneously; election runs independently per cluster so each host gets its own cluster's elected primary.

The split:

- **Per-node state** runs on every node (modules, packages, snap install, `.microk8s.yaml`, Calico autodetect patch, registry mirrors, `/etc/hosts`, group membership, kubectl alias).
- **Cluster-scoped state** runs only on the primary — addons, capability labels, the MetalLB pool spec, and the CoreDNS Corefile. This avoids racing two `microk8s enable X` invocations or two competing patches against the same kube-system state.
- **Join** runs only on non-primary nodes. The role checks `high-availability.nodes` on the joining node; count > 1 means it's already in the cluster, count == 1 means it's still on its post-install solo cluster and needs to join. The join token comes from `microk8s add-node --format json --token-ttl 60` issued on the primary via `delegate_to`; the URL it returns is consumed immediately by `microk8s join` on the secondary. Tokens are single-use and short-TTL so they don't survive logs or process lists.

The label, MetalLB, and CoreDNS tasks all run `kubernetes.core.k8s` on the primary; that requires the `python3-kubernetes` apt package, which `prereqs.yml` installs on the primary only.

For single-node clusters (`k8s_dev` today, `wrkdevk8s` is the only host) election picks the only host; the join branch is a no-op.

Standalone playbooks that don't apply the role (`update-k8s.yml`, `evict-k8s.yml`, `refresh-k8s-addons.yml`) call `tasks_from: elect-primary` in a pre-flight play before any task that references `microk8s_primary_host`.

## Inventory contract

The role asserts that the four cluster-CIDR variables are set; per-cluster `group_vars/k8s_<cluster>.yml` is the right place. Each cluster (prd, dev, scratch) carries its own values — the defaults in `defaults/main.yml` are deliberately empty so a missing config fails loud rather than installing onto the wrong subnet.

| Variable | Per-cluster | Default | Notes |
|---|---|---|---|
| `microk8s_channel` | optional | `1.32/stable` | Pinned per `decisions.md` "k8s version policy." |
| `microk8s_calico_iface` | optional | `{{ '{{' }} ansible_default_ipv4.interface {{ '}}' }}` | Auto-resolves at role-apply time to the NIC carrying the default route. Override only if pod traffic should ride a non-default-route NIC. |
| `microk8s_cluster_cidr_v4` | **required** | `""` | E.g. `172.16.0.0/16` (prd) or `172.20.0.0/16` (scratch). |
| `microk8s_service_cidr_v4` | **required** | `""` | E.g. `172.17.0.0/16` (prd). |
| `microk8s_cluster_cidr_v6` | **required** | `""` | IPv6 cluster CIDR (`/64`). |
| `microk8s_service_cidr_v6` | **required** | `""` | IPv6 service CIDR (`/108`). |
| `microk8s_extra_sans` | optional | `[]` | API-server cert SANs, typically the service gateway IP. |
| `microk8s_addons` | optional | `[dns, helm, helm3, metrics-server]` | Bare addon names. Order matters; `community` must precede community-namespaced addons. The `metallb` addon's required IP-range arg is added inline by the role; inventory just lists `metallb`. |
| `microk8s_users` | optional | `[]` | OS users added to the `microk8s` group. |
| `microk8s_primary_host` | auto-elected | `""` | **Computed at runtime** by `tasks/elect-primary.yml` — do not set in inventory. |
| `k8s_node_labels` (per host, in `host_vars`) | optional | unset → `{}` | Map of capability labels to apply to that node's `Node` object. Keys/values are passed through verbatim to a strategic-merge patch — only labels you list are reconciled; existing kubernetes-managed and hand-applied labels are untouched. |
| `microk8s_metallb_namespace` | optional | `metallb-system` | Namespace where the addon installs the controller and creates the pool / advertisement. |
| `microk8s_metallb_pool_name` | optional | `default-addresspool` | Name of the `IPAddressPool` to upsert — matches the resource the `metallb` addon creates, so the reconcile lands on the addon's resource and overwrites its colon-arg range. |
| `microk8s_metallb_advertisement_name` | optional | `default-advertise-all-pools` | Name of the `L2Advertisement` to upsert — same rationale, matches the addon's auto-created advertisement. |
| `microk8s_metallb_pool_addresses` | optional | `[]` | List of CIDRs / `start-end` ranges (IPv4 and/or IPv6) — source of truth for the pool. Empty skips the task. |
| `microk8s_registry_mirrors` | optional | `[]` | List of `<host>:<port>` strings; per entry, a `certs.d/<host>:<port>/hosts.toml` is rendered pointing at `http://<entry>` with pull+resolve capabilities. |
| `microk8s_etc_hosts_entries` | optional | `[]` | List of `<ip> <name1> [name2 ...]` strings managed as a single marked block in `/etc/hosts`. Empty list removes the block. |
| `microk8s_coredns_hosts` | optional | `[]` | List of `{ip, names}` entries for the CoreDNS `hosts { ... fallthrough }` block. Reconciled on the primary. |
| `microk8s_coredns_templates` | optional | `[]` | List of `{domain, answer_a, ttl}` entries; each renders a `template IN A <domain>` block answering any subdomain with `<answer_a>`. Reconciled on the primary. |
| `microk8s_manage_apiserver_vip` | optional | `false` | `true` folds in a Keepalived VIP for the kube-apiserver. Set only on the 3-node prd cluster — `tasks/keepalived.yml` reads the VIP/VRID from `group_vars/all/vips.yml` and the `vrrp_auth_password` secret from there. The single-node dev cluster and the scratch fleet leave it `false`. |

## Idempotency notes

- The launch-config template renders to the same content on every run; subsequent applies are no-ops once it's in place. (Recall it only matters at first init — editing it post-install changes nothing in the cluster.)
- The Calico patch uses `replace` on `"first-found"` → `"interface=<iface>"`. After the first run the pattern is gone, so the task is a no-op and the handler doesn't fire.
- Addons enable only when missing from `microk8s status --format yaml`'s enabled list. Re-running the role against an already-converged node reports `changed=0`.
- The label patch reads each peer's existing `Node.metadata.labels` before writing; a strategic-merge patch with the same labels reports `changed: false`. Hosts with no `k8s_node_labels` declared are skipped entirely.
- The MetalLB `IPAddressPool` / `L2Advertisement` are upserted via `state: present` over the resources the `metallb` addon creates. First reconcile after addon enable replaces the addon's inline sentinel range; subsequent runs with the same `microk8s_metallb_pool_addresses` report `changed: false`.
- The `kubectl` alias is guarded by a `stat` so `snap alias` only runs when `/snap/bin/kubectl` is absent.
- The kube-apiserver VIP delegates to the `keepalived` role: `keepalived.conf` renders to the same content every run, so a converged node reports `changed=0` and the restart handler doesn't fire. The VIP include runs last in `main.yml`, after join, so the node is already serving the API before it can win the VRRP election.

## What this role doesn't do (yet)

- **Removal of labels** outside `k8s_node_labels`. The strategic-merge patch only adds/updates the keys we list; legacy labels (e.g. `size=large` on prd) need a one-shot operator step or a follow-up task to delete. Tracked with the Phase 4a HelmCharts migration gate.
