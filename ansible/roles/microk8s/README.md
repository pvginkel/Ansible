# `microk8s` role

Installs and configures microk8s on a k8s node. Lands an Ubuntu host at "ready to host workloads" — kernel modules, Ceph CSI client tooling, the snap pinned to a channel, the launch-config (`.microk8s.yaml`) with cluster CIDRs and extraSANs, the Calico interface autodetect patch, and the configured addon set. OS users get added to the `microk8s` group and `kubectl` is aliased system-wide.

## Scope

This role covers install, idempotent multi-node join, capability-label reconciliation, MetalLB pool reconciliation, registry-mirror config, and full-Corefile CoreDNS reconciliation. Node-local `/etc/hosts` entries are managed by the `baseline` role via `baseline_etc_hosts_entries` (set in `group_vars/k8s_*.yml`). The set of moving parts handled today:

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
| CoreDNS Corefile (primary only) | full-Corefile authoritative render via `kubernetes.core.k8s`, driven by `microk8s_coredns_hosts` and `microk8s_coredns_templates`; notifies a `kubectl rollout restart deployment/coredns` handler |
| Users | `user` module appends membership to the `microk8s` group; `~/.kube` per user |
| `kubectl` alias | `snap alias microk8s.kubectl kubectl`, guarded by a stat |
| kube-apiserver VIP | `include_role: keepalived` rendering a plain-VRRP instance for `kubernetes-api.home`; per-node, gated on `microk8s_manage_apiserver_vip` (see `tasks/keepalived.yml`) |
| kube-apiserver TLS leaf | `tasks/internal_tls.yml` includes the `internal_tls` role for a homelab-CA leaf, served additively via the apiserver's `--tls-sni-cert-key`; per-node, gated on `microk8s_apiserver_homelab_sans` |

## Primary vs. secondary nodes

`microk8s_primary_host` is **elected at runtime** per cluster — see [`tasks/elect-primary.yml`](tasks/elect-primary.yml). The election picks (in priority): the lowest-hostname node already in a working cluster, then the lowest-hostname running-solo node, then the alphabetical first node in the `k8s_<env>` group as a greenfield seed. This means rebuilding the labeled-primary doesn't strand the surviving cluster members — the next role apply elects one of them.

Cluster boundary is the inventory `k8s_<env>` child group each host belongs to. Under `inventories/prd`, `hosts: k8s` resolves to `k8s_prd` and `k8s_dev` simultaneously; election runs independently per cluster so each host gets its own cluster's elected primary.

The split:

- **Per-node state** runs on every node (modules, packages, snap install, `.microk8s.yaml`, Calico autodetect patch, registry mirrors, group membership, kubectl alias).
- **Cluster-scoped state** runs only on the primary — addons, capability labels, the MetalLB pool spec, and the CoreDNS Corefile. This avoids racing two `microk8s enable X` invocations or two competing patches against the same kube-system state.
- **Join** runs only on non-primary nodes. The role checks `high-availability.nodes` on the joining node; count > 1 means it's already in the cluster, count == 1 means it's still on its post-install solo cluster and needs to join. The join token comes from `microk8s add-node --format json --token-ttl 60` issued on the primary via `delegate_to`; the URL it returns is consumed immediately by `microk8s join` on the secondary. Tokens are single-use and short-TTL so they don't survive logs or process lists.

The label, MetalLB, and CoreDNS tasks all run `kubernetes.core.k8s` on the primary; that requires the `python3-kubernetes` apt package, which `prereqs.yml` installs on the primary only.

For single-node clusters (`k8s_dev` today, `srvk8sdev` is the only host) election picks the only host; the join branch is a no-op.

Standalone playbooks that don't apply the role (`update-k8s.yml`, `evict-k8s.yml`, `refresh-k8s-addons.yml`) call `tasks_from: elect-primary` in a pre-flight play before any task that references `microk8s_primary_host`.

## Worker-only nodes

A host with `microk8s_worker_only: true` in its `host_vars` joins the cluster as a **worker** — `microk8s join <url> --worker` — staying outside the dqlite/HA control-plane quorum. It runs kubelet plus the apiserver proxy; it does *not* run the apiserver, controller-manager, scheduler, or datastore. (Today: `srvk8s4`, the KubeCoder high-performance node.)

The role adapts in four places:

- **Join path** (`tasks/join.yml`) — appends `--worker` to the join command. Idempotency can't use `high-availability.nodes` (no dqlite on a worker), so the worker path counts cluster Nodes via `microk8s kubectl get nodes` instead: 1 = still solo, >1 = joined. The post-join readiness gate likewise polls the node's Ready condition through the proxy rather than `microk8s status --wait-ready`, whose worker behaviour is version-dependent.
- **Primary election** (`tasks/elect-primary.yml`) — worker-only hosts are dropped from the candidate set, so a worker can never be elected primary (it couldn't mint join tokens or reconcile cluster-scoped state).
- **kube-apiserver concerns** (`tasks/main.yml`) — the Keepalived VIP and the SNI TLS leaf are skipped on workers; there is no local apiserver to front or to add a cert flag to. Both remain gated on their existing per-cluster opt-ins *and* `not microk8s_worker_only`.
- **Calico cni.yaml patch** (`tasks/network.yml`) — skipped on workers. The autodetect method is a cluster-wide DaemonSet env var the control plane owns; a worker has no local `cni.yaml` to patch and its `calico-node` pod inherits the cluster setting.

Everything else — kernel modules, Ceph client packages, the snap install, registry mirrors, the `microk8s` group membership / kubectl alias — runs on a worker exactly as on a control-plane node. Capability labels declared in the worker's `k8s_node_labels` are still applied (by the elected primary's reconcile), so a worker can carry `performance: high` and the like.

## Node taints / dedicated nodes

`k8s_node_taints` (per host, in `host_vars`) makes a node **dedicated** — `tasks/taints.yml` reconciles the listed taints from the elected primary, the same place labels are applied. Each entry is `{key, value (optional), effect}`:

```yaml
k8s_node_taints:
  - key: homelab.local/performance
    value: high
    effect: NoSchedule
```

It's applied additively via `kubectl taint --overwrite`: the declared taint is set by key, and Kubernetes' own node-condition taints (`not-ready`, `unreachable`, …) plus a cordon's `unschedulable` flag are left intact. A `spec.taints` patch would replace the whole list and strip those, so the role deliberately doesn't do that. Idempotent — `kubectl taint` prints `not changed` and skips the API write when the taint already matches. Like labels, the role **adds/updates but never removes**: dropping a taint from inventory needs a one-shot `kubectl taint nodes <node> <key>-`.

**A `NoSchedule` taint repels DaemonSets as well as regular pods.** `calico-node` and `kube-proxy` tolerate everything already, but any *other* workload that must run on the tainted node needs a matching toleration — and those tolerations live with the workloads (HelmCharts), not here. For `srvk8s4` (tainted `homelab.local/performance=high:NoSchedule` so only KubeCoder runs there) that means the KubeCoder pods **and** the cluster DaemonSets it depends on — Ceph CSI (mandatory: the controller mounts CephFS), node-exporter, the MetalLB speaker, SMB CSI — must tolerate the taint, or they won't schedule on the node. The label/affinity attracts the opt-in pods; the taint repels the rest.

Note the reconcile runs on the primary, so a `rebuild-k8s.yml` run (whose converge play targets only the rebuilt node) does **not** apply the taint — it lands on the next full `site-k8s.yml`. There's a short window after a rebuild's uncordon where the node is schedulable but not yet tainted; for a born-tainted node, add `--register-with-taints` to the kubelet args instead (not done today).

## Inventory contract

The role asserts that the four cluster-CIDR variables are set; per-cluster `group_vars/k8s_<cluster>.yml` is the right place. Each cluster (prd, dev, scratch) carries its own values — the defaults in `defaults/main.yml` are deliberately empty so a missing config fails loud rather than installing onto the wrong subnet.

| Variable | Per-cluster | Default | Notes |
|---|---|---|---|
| `microk8s_channel` | optional | `1.35/stable` | Homelab k8s base version; pinned per cluster per `decisions.md` "k8s version policy" (prd 1.35, dev 1.36). |
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
| `k8s_node_taints` (per host, in `host_vars`) | optional | unset → none | List of `{key, value?, effect}` taints to apply to that node, additively via `kubectl taint --overwrite`. See "Node taints / dedicated nodes" below. |
| `microk8s_metallb_namespace` | optional | `metallb-system` | Namespace where the addon installs the controller and creates the pool / advertisement. |
| `microk8s_metallb_pool_name` | optional | `default-addresspool` | Name of the `IPAddressPool` to upsert — matches the resource the `metallb` addon creates, so the reconcile lands on the addon's resource and overwrites its colon-arg range. |
| `microk8s_metallb_advertisement_name` | optional | `default-advertise-all-pools` | Name of the `L2Advertisement` to upsert — same rationale, matches the addon's auto-created advertisement. |
| `microk8s_metallb_pool_addresses` | optional | `[]` | List of CIDRs / `start-end` ranges (IPv4 and/or IPv6) — source of truth for the pool. Empty skips the task. |
| `microk8s_registry_mirrors` | optional | `[]` | List of `<host>:<port>` strings; per entry, a `certs.d/<host>:<port>/hosts.toml` is rendered pointing at `http://<entry>` with pull+resolve capabilities. |
| `microk8s_coredns_hosts` | optional | `[]` | List of `{ip, names}` entries for the CoreDNS `hosts { ... fallthrough }` block. Reconciled on the primary. |
| `microk8s_coredns_templates` | optional | `[]` | List of `{domain, answer_a, ttl}` entries; each renders a `template IN A <domain>` block answering any subdomain with `<answer_a>`. Reconciled on the primary. |
| `microk8s_manage_apiserver_vip` | optional | `false` | `true` folds in a Keepalived VIP for the kube-apiserver. Set only on the 3-node prd cluster — `tasks/keepalived.yml` reads the VIP/VRID from `group_vars/all/vips.yml` and the `vrrp_auth_password` secret from there. The single-node dev cluster and the scratch fleet leave it `false`. |
| `microk8s_apiserver_homelab_sans` | optional | `[]` | SANs for a homelab-CA TLS leaf served additively on the kube-apiserver via `--tls-sni-cert-key`. Set per cluster in `group_vars/k8s_*.yml`. Distinct from `microk8s_extra_sans`, which seeds microk8s's *own* cert at first boot — this leaf is separate and leaves the internal PKI untouched. Empty skips it. |
| `microk8s_worker_only` (per host, in `host_vars`) | optional | `false` | `true` joins this node with `microk8s join --worker` — outside the dqlite/HA quorum, no apiserver/datastore. See "Worker-only nodes" below. |

## Idempotency notes

- The launch-config template renders to the same content on every run; subsequent applies are no-ops once it's in place. (Recall it only matters at first init — editing it post-install changes nothing in the cluster.)
- The Calico patch uses `replace` on `"first-found"` → `"interface=<iface>"`. After the first run the pattern is gone, so the task is a no-op and the handler doesn't fire.
- Addons enable only when missing from `microk8s status --format yaml`'s enabled list. Re-running the role against an already-converged node reports `changed=0`.
- The label patch reads each peer's existing `Node.metadata.labels` before writing; a strategic-merge patch with the same labels reports `changed: false`. Hosts with no `k8s_node_labels` declared are skipped entirely.
- The MetalLB `IPAddressPool` / `L2Advertisement` are upserted via `state: present` over the resources the `metallb` addon creates. First reconcile after addon enable replaces the addon's inline sentinel range; subsequent runs with the same `microk8s_metallb_pool_addresses` report `changed: false`.
- The `kubectl` alias is guarded by a `stat` so `snap alias` only runs when `/snap/bin/kubectl` is absent.
- The kube-apiserver VIP delegates to the `keepalived` role: `keepalived.conf` renders to the same content every run, so a converged node reports `changed=0` and the restart handler doesn't fire. The VIP include runs last in `main.yml`, after join, so the node is already serving the API before it can win the VRRP election.
- The kube-apiserver TLS leaf is threshold-gated by the `internal_tls` role (re-issues only inside the renewal window) and the `--tls-sni-cert-key` arg is a `lineinfile` upsert, so a converged node reports `changed=0` and the kubelite restart doesn't fire.

## Watch-cache freeze recovery

`tasks/watchdog.yml` installs a per-node systemd timer (`dqlite-watchdog.timer`) that self-heals a frozen apiserver watch cache — the microk8s 1.34–1.35 `k8s-dqlite` watch-stall bug ([k8s-dqlite#364](https://github.com/canonical/k8s-dqlite/issues/364) / [microk8s#5386](https://github.com/canonical/microk8s/issues/5386)). Every few minutes each apiserver node probes its **own** apiserver for the freeze signature (cache-served vs quorum `resourceVersion` divergence on the controller-manager lease) and restarts `snap.microk8s.daemon-k8s-dqlite` only on that signature. Each node heals itself — no orchestrator, no cross-node coordination; `RandomizedDelaySec` jitters the nodes so they never restart in lockstep. Installed on apiserver nodes only (worker-only nodes are skipped). Tuning lives in the `microk8s_watchfreeze_*` / `microk8s_watchdog_*` defaults; the probe is `files/dqlite-watch-probe.sh`. Recovery is silent in `journalctl -u dqlite-watchdog`. Full background and manual fallback: [`docs/runbooks/dqlite-watch-freeze.md`](../../../docs/runbooks/dqlite-watch-freeze.md).

## What this role doesn't do (yet)

- **Removal of labels** outside `k8s_node_labels`. The strategic-merge patch only adds/updates the keys we list; legacy labels (e.g. `size=large` on prd) need a one-shot operator step or a follow-up task to delete. Tracked with the Phase 4a HelmCharts migration gate.
