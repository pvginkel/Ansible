# `baseline` role

Applies OS hygiene to a managed Ubuntu host. Ported from `/work/Obsidian/Linux.md` with Samba/winbind/NetBIOS removed and UFW skipped (the homelab runs on Ubuntu's default firewall posture).

## What it does

- Sets timezone to `Europe/Amsterdam` (override: `baseline_timezone`).
- Refreshes the apt cache; optionally runs `apt dist-upgrade` when `baseline_apt_dist_upgrade: true` (off by default — see "Updates" below for the longer story).
- Installs `qemu-guest-agent` and enables the service.
- Installs and enables `prometheus-node-exporter`. Universal across every managed host so the in-cluster Prometheus has uniform visibility. Pointing Prometheus at the new targets is a HelmCharts change, separate from this role.
- **Applies the host's OS update class** (per `baseline_os_update_class`; default `cluster`):
  - `cluster` — k8s/ceph nodes. Purges `unattended-upgrades`; the `update.yml` playbook owns drain+upgrade+reboot.
  - `standalone` — VMs like `srviac` (and future OpenBao hosts). Installs `unattended-upgrades` and drops `/etc/apt/apt.conf.d/99-unattended-upgrades-iac` to auto-reboot in a quiet window per `baseline_unattended_reboot_time` (default `03:00`; stagger across standalone hosts).
- Installs per-host extras via `baseline_extra_packages` (empty by default). Override in `host_vars/<host>.yml` or `group_vars/<group>.yml`:
  ```yaml
  baseline_extra_packages:
    - htop
    - tmux
    - jq
  ```
- Strips the execute bit from the noisy MOTD scripts (`10-help-text`, `50-motd-news`, `85-fwupd`, `90-updates-available`, `97-overlayroot`).
- Drops the same `.vimrc` into `/root` and `/home/pvginkel` (dark background, 4-space tabs).
- **Re-asserts `/etc/netplan/50-cloud-init.yaml`** when the host declares a `static_netplan` host_var. Mirrors the netplan that `terraform/prd/cloud-init.yaml.tftpl` writes on first boot, so a static-IP change in `vms.tf` plus a matching host_var update lands on the running host without a rebuild. Skipped when `static_netplan` is undefined. See `/work/AnsibleSpecs/decisions.md` "Cloud-init is a first-boot artefact".

## Updates

Baseline does not configure or run package upgrades on its own. The dedicated `update.yml` playbook (forthcoming) owns `apt update`/`full-upgrade`/conditional reboot, with `serial: 1` + drain/uncordon for k8s and Ceph nodes once those phases land. Until then, `baseline_apt_dist_upgrade: true` is the operator's manual hatch — set it on a host (or via `-e`) to force a one-time dist-upgrade through baseline. See `/work/AnsibleSpecs/decisions.md` "OS updates".

## Depends on

`bootstrap` — `pvginkel` must exist before we can drop a `.vimrc` into their home.

## Not in scope

- UFW / firewalling — left at Ubuntu defaults.
- Samba / winbind / NetBIOS name resolution — dropped; DNS handles name resolution.
- Jenkins pipeline SSH key on `~pvginkel/.ssh/authorized_keys` — deferred to Phase 4 (only k8s nodes need it).
