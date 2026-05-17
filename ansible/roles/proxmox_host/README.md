# `proxmox_host` role

Per-PVE-node configuration. Applied to every host in the `proxmox` group; some tasks are gated to a single cluster-writer node (default `pve`) because their target is the cluster-shared `/etc/pve` (pmxcfs).

## What it does

| Task | Where | Why |
|---|---|---|
| Sysctl `vm.swappiness=10`, `vm.dirty_bytes=256M`, `vm.dirty_background_bytes=128M` | every node | Caps dirty-page accumulation so vzdump to the slow `local-backup` HDD doesn't push the host into swap. See `/work/Obsidian/Proxmox.md` "RAM usage". |
| Issue the pveproxy TLS leaf from the homelab CA (`internal_tls`) | every node | User-facing Web UI / API certificate. step-ca JWK leaf for `<node>` + `<node>.home`, written to `/etc/pve/local/pveproxy-ssl.{pem,key}`; `systemctl reload pveproxy` on change. |
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

## pveproxy TLS

The role includes `internal_tls` to replace pveproxy's self-signed certificate with a homelab `step-ca` leaf (JWK provisioner). It is per-node, *not* cluster-writer gated: `/etc/pve/local` resolves to the node-private `/etc/pve/nodes/<node>/` directory, so each PVE node issues and serves its own leaf, SAN'd to its own short hostname and `.home` FQDN.

- **Only the user-facing cert moves.** The PVE *cluster* CA (`/etc/pve/pve-root-ca.pem`) and the `pve-ssl.*` node certificates — which sign cluster-internal traffic — are untouched.
- **Ownership is `root:www-data`, not `root:root`.** `/etc/pve` is pmxcfs (FUSE): it presents every file as `root:www-data 0640` and rejects `chown`/`chmod`. `root:www-data 0640` matches what pmxcfs already shows, so `internal_tls`'s `file` tasks stay idempotent; `root:root` would make them fail with `EPERM`. `pveproxy` runs as `www-data`.
- **Renewal** is threshold-gated by `internal_tls` (re-issue under 14 days left) on each `iac-scheduled-drift` cycle.
- The leaf key lands in pmxcfs, which replicates it to the other PVE nodes — inherent to how PVE stores `pveproxy-ssl.*`, and within the cluster's single root-trust domain.

## Notes

- Every change converges. A second run reports `changed=0`.
