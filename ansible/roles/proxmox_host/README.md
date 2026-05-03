# `proxmox_host` role

Per-PVE-node configuration. Applied to every host in the `proxmox` group; some tasks are gated to a single cluster-writer node (default `pve`) because their target is the cluster-shared `/etc/pve` (pmxcfs).

## What it does

| Task | Where | Why |
|---|---|---|
| Sysctl `vm.swappiness=10`, `vm.dirty_bytes=256M`, `vm.dirty_background_bytes=128M` | every node | Caps dirty-page accumulation so vzdump to the slow `local-backup` HDD doesn't push the host into swap. See `/work/Obsidian/Proxmox.md` "RAM usage". |
| Manage `/etc/pve/jobs.cfg` | cluster-writer only | Cluster-wide vzdump backup definition. pmxcfs propagates from one node. |
| Ensure `pvginkel@pam` exists in PVE with `PVEAdmin` on `/` | cluster-writer only | Operator account for the web UI / API. The Linux user is created by `bootstrap`; PAM auth on a PVE node maps `pvginkel@pam` to the local Linux account. |
| Remove pre-rotation `pvginkel` RSA from `/etc/pve/priv/authorized_keys` | cluster-writer only | Stale, superseded by the ed25519 in commit `a9f2ed5`. |
| Remove `root@pve3` from cluster `authorized_keys` | cluster-writer only | Orphaned — the fourth node was decommissioned. |
| Remove `tag-style: color-map=migrated:...` from `datacenter.cfg` | cluster-writer only | UI cosmetic for a tag no longer in use. |
| Delete `/root/{install.sh,swap-usage.sh,backup-monitor.sh,log.txt}` | cluster-writer only | Operator scratch from troubleshooting the swap-during-backup issue. |

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

## PAM operator user

`proxmox_host_pam_user` (default `pvginkel`) is created with role `proxmox_host_pam_user_role` (default `PVEAdmin`) on the cluster root. After the role applies, set a Linux password for the user once per node so PAM auth works:

```sh
ssh root@pve passwd pvginkel
```

(Per-node, not cluster-wide — `/etc/shadow` is local.) The repo holds no password.

## Notes

- Every change converges. A second run reports `changed=0`.
