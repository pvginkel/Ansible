# `baseline` role

Applies OS hygiene to a managed Ubuntu host. Ported from `/work/Obsidian/Linux.md` with Samba/winbind/NetBIOS removed and UFW skipped (the homelab runs on Ubuntu's default firewall posture).

## What it does

- Sets timezone to `Europe/Amsterdam` (override: `baseline_timezone`).
- Refreshes the apt cache; optionally runs `apt dist-upgrade` when `baseline_apt_dist_upgrade: true` (off by default — unattended-upgrades owns ongoing updates).
- Installs `qemu-guest-agent` and `unattended-upgrades`; enables the guest agent service.
- Installs per-host extras via `baseline_extra_packages` (empty by default). Override in `host_vars/<host>.yml` or `group_vars/<group>.yml`:
  ```yaml
  baseline_extra_packages:
    - htop
    - tmux
    - jq
  ```
- Strips the execute bit from the noisy MOTD scripts (`10-help-text`, `50-motd-news`, `85-fwupd`, `90-updates-available`, `97-overlayroot`).
- Drops the same `.vimrc` into `/root` and `/home/pvginkel` (dark background, 4-space tabs).
- Writes `/etc/apt/apt.conf.d/50unattended-upgrades` with auto-reboot at 02:00, removal of unused kernels and new unused deps, and the full set of allowed origins from `Linux.md`.

## Depends on

`bootstrap` — `pvginkel` must exist before we can drop a `.vimrc` into their home.

## Not in scope

- UFW / firewalling — left at Ubuntu defaults.
- Samba / winbind / NetBIOS name resolution — dropped; DNS handles name resolution.
- Jenkins pipeline SSH key on `~pvginkel/.ssh/authorized_keys` — deferred to Phase 4 (only k8s nodes need it).
