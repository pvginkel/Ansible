# Phase 2 — Proxmox host management

**Status**: ✅ Done

## What landed

- [`ansible/playbooks/adopt.yml`](../../ansible/playbooks/adopt.yml) — onboarding playbook for non-cloud-init'd hosts. Captures the host's existing ed25519 SSH host key, drops the `ansible` (and optionally `pvginkel`) users, sets passwordless sudo. See [`docs/runbooks/adoption.md`](../runbooks/adoption.md).
- [`ansible/roles/proxmox_host/`](../../ansible/roles/proxmox_host/) — PVE-node configuration: sysctl tuning, per-VM CPU affinity reconciliation, cluster vzdump backup job, `pvginkel@pam` PAM operator user with `PVEAdmin` on `/`, plus a handful of transitional cleanups (orphaned `root@pve3` key, pre-rotation `pvginkel` RSA, retired `migrated` UI tag, `/root` scratch scripts on `pve`).
- Inventory: `pve_vms` (data-only) and `managed` (site.yml target) parent groups in `inventories/prd/hosts.yml`. Per-VM host_vars carry `vm_id`, `pve_node`, `workload_class`. Workload-class → core-range map in `host_vars/pve.yml`.
- `bootstrap` and `baseline` role updates: `sudo` install (PVE doesn't ship it), `qemu-guest-agent` gated on `ansible_virtualization_role == 'guest'` (was Ubuntu-distro nominal), Debian/bookworm in galaxy_info platforms.
- All three PVE nodes adopted; full `site.yml --check --diff -l proxmox` is clean.

## Operationally useful afterwards

- **Adopting a new host** — `docs/runbooks/adoption.md`. The same playbook works for any non-cloud-init'd Linux host, not just PVE.
- **Cluster-writer concept** — `proxmox_host_cluster_writer` (default `pve`) is the single node the role uses to write to the shared `/etc/pve` namespace. If `pve` is ever replaced, override the variable in inventory before the run.
- **Affinity declaration** — adding a new VM that should be CPU-pinned: list it under `pve_vms_unmanaged` (or whatever functional group fits), add `inventories/prd/host_vars/<name>.yml` with `vm_id`, `workload_class`, and (if not on `pve`) `pve_node`.
- **Setting an operator password on PVE** — `bootstrap` does not set a password for `pvginkel`, and `pvginkel@pam` uses Linux PAM. Run `passwd pvginkel` once per node after first apply, before either interactive sudo or the PVE web UI work.
- **Transitional cleanups due for removal** — the four cleanup tasks in `proxmox_host` (pre-rotation `pvginkel` RSA, `root@pve3`, `migrated` tag-style, `/root/{install.sh,swap-usage.sh,backup-monitor.sh,log.txt}`) have converged on every node and won't recur. Per `docs/decisions.md` "Transitional cleanup tasks age out", remove them in a follow-up commit when convenient.
