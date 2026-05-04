# Phase 4b1 — Rebuild prerequisites (registry, CoreDNS, ZFS)

**Status**: ✅ Done

## Result

Closed the bring-up gaps that today's live nodes carry as out-of-band hand-edits, so a freshly cloud-init'd k8s VM under Phase 4c reaches a fully functional steady state from `rebuild-k8s.yml` alone — no manual `kubectl edit cm coredns`, no manual `tee` into `/var/snap/microk8s/current/args/certs.d/`, no missing `zpool` binary on the node that carries the passthrough pool.

What the `microk8s` role now reconciles, driven by inventory:

- **Containerd registry mirrors** (`microk8s_registry_mirrors`) — renders `/var/snap/microk8s/current/args/certs.d/<host>/hosts.toml` per entry; notifies a `microk8s stop && start` handler so containerd reloads. Set to `[registry:5000, registry-dev:5000]` for prd, `[registry-dev:5000]` for dev, empty on scratch.
- **Node-local `/etc/hosts`** (`microk8s_etc_hosts_entries`) — `blockinfile`-managed, mandatory for cold-boot independence from LAN DNS. prd pins `172.17.0.3 registry`; dev pins `192.168.178.43 registry-dev` (LAN-direct).
- **CoreDNS Corefile** (`microk8s_coredns_hosts`, `microk8s_coredns_templates`) — primary-only, full-Corefile authoritative render via `kubernetes.core.k8s` with content-compare drift; notifies a `kubectl rollout restart deployment/coredns` handler. prd carries the registry alias, the three Ceph mon hostnames at hardcoded IPs (`10.1.0.24/.25/.26`), and the `webathome.org` / `ginbov.nl` template rewrites pointing at `10.2.1.7`. dev's lists are empty.
- **`zfsutils-linux`** in `playbooks/group_vars/k8s.yml`'s `baseline_extra_packages` — every k8s node gets the package so `rebuild-k8s.yml`'s `zpool import` step has the binary regardless of which node ends up with a passthrough pool.
- **Per-cluster runtime primary election** (`tasks/elect-primary.yml`) — replaces the static `microk8s_primary_host` inventory key with discovery: lowest-hostname in-cluster member → lowest-hostname running-solo → alphabetical seed. Cluster boundary is the `k8s_<env>` child group each host belongs to. Standalone playbooks (`update-k8s.yml`, `evict-k8s.yml`, `rebuild-k8s.yml`, `refresh-k8s-addons.yml`) call `tasks_from: elect-primary` in a pre-flight play before any task that references the primary. `python3-kubernetes` lands on every k8s node (not just the elected primary) so a freshly-rebuilt node is always election-eligible.

Exercised against `inventories/scratch` (rebuild-of-primary case included — `wrkscratchk8s1` rebuilt fresh, election promoted `wrkscratchk8s2`, k1 joined k2) and `inventories/prd --limit k8s_dev` (live `wrkdevk8s` reconciled clean; the legacy `10.153.182.3 registry` CoreDNS hosts entry was dropped by the authoritative render). Final `--check --diff` reports `changed=0` on both.

## Ceph IPs are static infrastructure

Phase 9's plan originally queued `srvceph1/2/3` for the dynamic `homelab_dns_reservation` resource. Reverted in this phase: the registry container depends on Ceph storage to boot, dnsmasq runs in-cluster behind the registry, and any chain that puts Ceph addressing behind the dynamic API creates a cold-boot ordering failure. `terraform/modules/managed-vm` carries a `static_ip` opt-out flag set on the three Ceph entries in `terraform/prd/vms.tf`; their IPs live exclusively in HelmCharts `configs/{prd,dev}/dnsmasq.yaml`. See `decisions.md` "Ceph nodes are static infrastructure" and `docs/plans/02-dns-reservation-provider.md`.

## prd not yet exercised

The role + inventory landed against live dev only. Live prd's first contact with the new role happens during the 4c rebuilds — each rebuilt VM picks up the role from a fresh state, validating 4b1's logic in the process. Avoids touching live prd twice (once for 4b1 reconcile, once for 4c rebuild). The `daemon.json` runtime config item from the original phase scope was dropped: no Docker host exists in the managed inventory; revisit if/when one appears.

## Follow-up issues surfaced during the exercise

Captured here so 4c (or the next role-touch phase) can fold them in:

- **MetalLB CRD/webhook race** — fresh `microk8s enable metallb` returns before the CRDs are queryable from the discovery cache, and the validating webhook can be transiently unreachable when CoreDNS rollout-restarts. Both manifest as `kubernetes.core.k8s` errors against the IPAddressPool. Defensive `kubectl wait` for the CRD + the controller deployment at the top of `tasks/metallb.yml` covers both.
- **`elect-primary.yml` retries** — a freshly-reset or just-rebooted node can race the election if its `microk8s` daemon isn't yet up: `microk8s status` fails, the node classifies `down`, and tier-3 fallback may pick the wrong primary. Add a small retry loop on the status read.
- **`join.yml` idempotency is local-only** — checks the joining node's own `high-availability.nodes` count. Doesn't cross-verify membership from the primary's view, so a real cluster split where each side thinks it's joined would not be detected. Cross-check via `microk8s kubectl get nodes` on the elected primary.
- **Audit other primary-only gates** — `prereqs.yml`'s `python3-kubernetes` install was originally gated on `inventory_hostname == microk8s_primary_host`, which became fragile under dynamic election. Sweep for similar gates that should be every-node now.

## Continuation

[`phase-4c-microk8s-rebuild-execution.md`](phase-4c-microk8s-rebuild-execution.md) drives the four prd k8s VM rebuilds.
