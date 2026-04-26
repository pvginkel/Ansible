# `proxmox_host` role

Per-PVE-node configuration. Applied to every host in the `proxmox` group; some tasks are gated to a single cluster-writer node (default `pve`) because their target is the cluster-shared `/etc/pve` (pmxcfs).

## What it does

| Task | Where | Why |
|---|---|---|
| Sysctl `vm.swappiness=10`, `vm.dirty_bytes=256M`, `vm.dirty_background_bytes=128M` | every node | Caps dirty-page accumulation so vzdump to the slow `local-backup` HDD doesn't push the host into swap. See `/work/Obsidian/Proxmox.md` "RAM usage". |
| Reconcile per-VM CPU affinity (`qm set <vmid> --affinity <range>`) | nodes that define `proxmox_workload_affinity_cores` (just `pve` today) | Pins interactive workloads to cores 0-11 and background workloads to 12-19. The Terraform user can't set the affinity field (root-only), so Ansible owns it post-provision. |
| Manage `/etc/pve/jobs.cfg` | cluster-writer only | Cluster-wide vzdump backup definition. pmxcfs propagates from one node. |
| Remove pre-rotation `pvginkel` RSA from `/etc/pve/priv/authorized_keys` | cluster-writer only | Stale, superseded by the ed25519 in commit `a9f2ed5`. |
| Remove `root@pve3` from cluster `authorized_keys` | cluster-writer only | Orphaned — the fourth node was decommissioned. |
| Remove `tag-style: color-map=migrated:...` from `datacenter.cfg` | cluster-writer only | UI cosmetic for a tag no longer in use. |
| Delete `/root/{install.sh,swap-usage.sh,backup-monitor.sh,log.txt}` | cluster-writer only | Operator scratch from troubleshooting the swap-during-backup issue. |

## Inventory contract

Each VM in `groups['pve_vms']` must declare:

- `vm_id` — integer Proxmox VMID.
- `pve_node` — short name of the PVE node hosting it (`pve`, `pve1`, `pve2`).
- `workload_class` — `interactive` or `background`. Required even on hosts whose `pve_node` is unzoned (pve1/pve2) so the data is intrinsic to the VM.

Each PVE node that should reconcile affinity must declare `proxmox_workload_affinity_cores` in its host_vars:

```yaml
proxmox_workload_affinity_cores:
  interactive: "0-11"
  background: "12-19"
```

If the variable is undefined on a node, the affinity block no-ops there.

## Cluster-writer concept

`proxmox_host_cluster_writer` (default `pve`) is the single node that writes to `/etc/pve/*`. pmxcfs is cluster-shared, so writing from one node propagates to the rest — but writing concurrently from multiple nodes is a recipe for races. Tasks that touch shared files are gated `when: inventory_hostname == proxmox_host_cluster_writer`.

If `pve` ever needs to be replaced, override `proxmox_host_cluster_writer` in inventory before the run.

## Backup job tuning

The vzdump job is templated from variables:

```yaml
proxmox_host_backup_job_id: backup-774ec731-f7bd  # Pin to existing UUID — backup files reference it.
proxmox_host_backup_node: pve
proxmox_host_backup_storage: local-backup
proxmox_host_backup_schedule: "4:00"
proxmox_host_backup_keep: 3
proxmox_host_backup_mailto: pvginkel@gmail.com
```

`proxmox_host_backup_job_id` is intentionally a fixed UUID matching what the cluster has today. Changing it creates a new job entry alongside the existing one — coordinate that change with backup-file retention before flipping it.

## Notes

- Every change converges. A second run reports `changed=0`.
- Affinity changes via `qm set` apply on the next VM start; running VMs keep their old affinity until restarted (or live-migrated). The `proxmox_host` role doesn't reboot anything.
