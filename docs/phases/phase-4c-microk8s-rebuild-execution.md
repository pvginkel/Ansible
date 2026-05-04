# Phase 4c — k8s rebuild execution (first worker + scaffolding pivot)

**Status**: ✅ Done

## Result

Drove worker rebuild #1 (`srvk8ss1` → `srvk8s2`) end-to-end and resolved a structural gap in the from-scratch shape that surfaced mid-rebuild. Cluster is back to three Ready prd workers (`srvk8sl1`, `srvk8s2`, `srvk8ss2`); srvk8s2 is on the new shape and soaking. Phase 4d picks up the remaining three rebuilds and the close-the-parity-event commit.

## What happened

1. **Pre-flight eviction on `srvk8ss2`** — exercised the eviction path against a live worker before any rebuild. Surfaced a bare-Pod drain failure (Jenkins K8s plugin pod, no controller). Resolved by waiting out the in-flight build; rerun was clean. No code change taken; option B (`--force`) noted for later if this becomes a recurring blocker.

2. **`srvk8ss1` → `srvk8s2` rebuild** — followed the runbook through eviction + leave + remove-node + inventory rename, then hit two TF surprises before the first apply landed:
   - `dns_ipv4` output errored on Ceph modules (`var.static_ip ? null : reservation[0].ipv4` failed because Terraform evaluates the unselected ternary branch's tuple-index against an empty list). Fixed: `value = one(homelab_dns_reservation.this[*].ipv4)`.
   - `proxmox_download_file` refused to overwrite the Ubuntu cloud image scratch had landed earlier on `pve`. Fixed: `overwrite_unmanaged = true`.
   - `terraform plan` proposed destroying all three orphan k8s VM module instances (`srvk8sl1/ss1/ss2`) despite `-target` — `for_each` orphan reconciliation isn't suppressed by targeting. Fixed: `terraform state rm` on the three orphans before the first apply (a no-op on PVE; the live VMs kept running, TF just stopped tracking them).

3. **Static-IP pivot for k8s nodes** — first apply created srvk8s2 with eth0 on dnsmasq DHCP and DHCP-pushed cluster DNS at `10.2.1.2/3`. The workload-VLAN NIC (`enp6s19`) stayed DOWN because cloud-init only configured the first NIC, so containerd couldn't resolve external registries (the LB IP it was being told to use was unreachable from a host without a workload-VLAN address). Diagnosed against the live srvk8sl1 / srvk8ss2 netplan and pivoted to the same shape Ceph already uses:
   - `terraform/modules/managed-vm/variables.tf` — extended `network_devices` with optional `addresses` / `gateway` / `accept_ra` / `nameservers` / `search`.
   - `terraform/prd/cloud-init.yaml.tftpl` — renders `/etc/netplan/50-cloud-init.yaml` from the per-NIC IP fields when at least one NIC carries them; `runcmd` does `netplan generate && netplan apply`.
   - `terraform/prd/main.tf` — threads the IP fields into the templatefile call.
   - `terraform/prd/vms.tf` — `static_ip = true` on all four from-scratch k8s VMs; `.27` / `.28` / `.29` / `.17` IPv4 + matching IPv6 across all three networks per the live nodes' last-octet convention.
   - `docs/decisions.md` — extended the "bring-up-tier hosts" rationale from Ceph to k8s nodes (host the registry + dnsmasq pods themselves; can't bootstrap their own networking from services they're required to bring up).
   
   srvk8s2 was destroyed and recreated with the new cloud-init via `terraform apply -replace`; first boot's cloud-init wrote the static netplan, `netplan apply` flipped eth0 from the temporary DHCP IP to `10.1.0.28`, and the role apply landed clean.

4. **HelmCharts static-hosts** — operator added entries for srvk8s1/2/3 + wrkdevk8s in lockstep with the static-IP pivot. Old `srvk8ss1` entry retired.

5. **`rebuild-k8s.yml` against srvk8s2** — bootstrap + baseline + microk8s; node joined the cluster as a worker. `--check --diff` reports `changed=0` post-rebuild.

## Carry-over to 4d

- Three rebuilds remain: srvk8s3, srvk8s1, wrkdevk8s. Static-IP scaffolding is in place; subsequent applies use the originally-planned simpler flow (no `-replace`, no template-fix detour).
- VMID 104 (old srvk8ss1) is shut down on `pve1`, kept as escape hatch.
- Operator-side `qm destroy 104`, `qm destroy 107`, and the close-the-parity-event commit (retire adoption known_hosts files) move to 4d.
- `srvk8s2` is in soak; morning checks + label-parity verification documented in [`phase-4d-microk8s-rebuild-completion.md`](phase-4d-microk8s-rebuild-completion.md).

## Follow-ups surfaced

- **Pre-drain hand-off readiness check** — `kubectl rollout status` returned 0 before the new pod was Ready under the keycloak/keycloak-db restart. Captured in `docs/plans/07-pre-drain-readiness-check.md`. Not a blocker for the rebuilds.
- **Runbook updates** for the static-IP pivot, the orphan `state rm` step, and the cloud-init resource targeting — listed in 4d's "Runbook + decisions.md follow-ups" so they land alongside the phase close.
- **Bare-Pod drain** — Jenkins build agents block `kubectl drain` until the build finishes. Acceptable today (wait + retry); revisit if it blocks a real maintenance window.
