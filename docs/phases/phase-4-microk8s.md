# Phase 4 — microk8s role (scratch exercise)

**Status**: ✅ Done

## Result

Built and exercised the `microk8s` role end-to-end on a fresh two-node scratch cluster (`wrkscratchk8s1` + `wrkscratchk8s2`, `inventories/scratch`). The role lands an Ubuntu host at "Ready microk8s with the configured addons enabled":

- Kernel modules + Ceph client tooling.
- Snap install pinned to `1.32/stable` (per `decisions.md` "k8s version policy").
- `.microk8s.yaml` (CIDRs + extraSANs) staged before snap install so first-init reads it.
- Calico autodetect patched into the snap-shipped `cni.yaml`. The interface is derived per-host from `ansible_default_ipv4.interface` so cloud-init'd VMs (`eth0`) and manually-installed prod nodes (`ens18`) both resolve correctly without per-cluster overrides.
- Addons enabled by inventory list, idempotent — parses `microk8s status --format yaml` to compute what's missing.
- Idempotent multi-node join via a primary/secondary split: `microk8s_primary_host` per cluster designates one node; non-primary nodes call `microk8s add-node` on the primary via `delegate_to`, consume the URL with `microk8s join`. Idempotency anchored on `high-availability.nodes` count > 1.
- OS user added to the `microk8s` group + `kubectl` aliased system-wide. Snap-busy retry on the alias step covers the post-join `service-control` race.

`--check --diff` and a real apply re-run report `changed=0` cleanly on both scratch nodes. The role is the artifact going into Phase 4a.

The scratch terraform was refactored from a single-VM standalone config to a `for_each`-over-`local.vms` shape using the existing `modules/managed-vm`. VMID range 900–909 reserved for the scratch fleet (per `decisions.md` "VMID convention").

## Source material absorbed

`/work/Obsidian/Kubernetes.md` (procedural runbook) and `/work/KubernetesConfig` (`.microk8s.yaml` launch configs, MetalLB IPAddressPool YAMLs, `installatie/*.md`) — both substantially absorbed into the role and inventory. The MetalLB IPAddressPool reconciliation lands in Phase 4a (closing the absorption); after Phase 4a, KubernetesConfig is archived.

## Deferred to Phase 4a

The role's "ready to host workloads" surface is missing two reconciliation pieces — **capability labels** (`homelab.local/storage`, `homelab.local/performance`) and **MetalLB IPAddressPool / L2Advertisement** — both kubernetes.core-driven and best exercised against real cluster destinations rather than scratch. Adopting the live prod and dev clusters under the role, the drain-aware upgrade playbook, the per-VM TF module extension to the from-scratch shape, the rebuild playbook with `serial: 1`, and the actual rebuild of all four k8s VMs (the parity event per `decisions.md` "Adoption is a waypoint") all live in `docs/phases/phase-4a-microk8s-rebuild.md`.
