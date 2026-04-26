# `baseline` role

Applies OS hygiene to a managed Ubuntu host. Ported from `/work/Obsidian/Linux.md` with Samba/winbind/NetBIOS removed and UFW skipped (the homelab runs on Ubuntu's default firewall posture).

## What it does

- Sets timezone to `Europe/Amsterdam` (override: `baseline_timezone`).
- Refreshes the apt cache; optionally runs `apt dist-upgrade` when `baseline_apt_dist_upgrade: true` (off by default — see "Updates" below for the longer story).
- Installs `qemu-guest-agent` and enables the service.
- **Purges `unattended-upgrades`** — Ansible owns OS updates, not the OS. Removing the package (vs. deleting only our config) prevents Ubuntu's defaults from silently re-enabling background updates.
- Installs per-host extras via `baseline_extra_packages` (empty by default). Override in `host_vars/<host>.yml` or `group_vars/<group>.yml`:
  ```yaml
  baseline_extra_packages:
    - htop
    - tmux
    - jq
  ```
- Strips the execute bit from the noisy MOTD scripts (`10-help-text`, `50-motd-news`, `85-fwupd`, `90-updates-available`, `97-overlayroot`).
- Drops the same `.vimrc` into `/root` and `/home/pvginkel` (dark background, 4-space tabs).

## Updates

Baseline does not configure or run package upgrades on its own. The dedicated `update.yml` playbook (forthcoming) owns `apt update`/`full-upgrade`/conditional reboot, with `serial: 1` + drain/uncordon for k8s and Ceph nodes once those phases land. Until then, `baseline_apt_dist_upgrade: true` is the operator's manual hatch — set it on a host (or via `-e`) to force a one-time dist-upgrade through baseline. See `docs/decisions.md` "OS updates".

## Depends on

`bootstrap` — `pvginkel` must exist before we can drop a `.vimrc` into their home.

## Not in scope

- UFW / firewalling — left at Ubuntu defaults.
- Samba / winbind / NetBIOS name resolution — dropped; DNS handles name resolution.
- Jenkins pipeline SSH key on `~pvginkel/.ssh/authorized_keys` — deferred to Phase 4 (only k8s nodes need it).
