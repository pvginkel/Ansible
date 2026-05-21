# `baseline` role

Applies OS hygiene to a managed Ubuntu host. Ported from `/work/Obsidian/Linux.md` with Samba/winbind/NetBIOS removed and UFW skipped (the homelab runs on Ubuntu's default firewall posture).

## What it does

- Sets timezone to `Europe/Amsterdam` (override: `baseline_timezone`).
- Refreshes the apt cache; optionally runs `apt dist-upgrade` when `baseline_apt_dist_upgrade: true` (off by default — see "Updates" below for the longer story).
- Installs `qemu-guest-agent` and enables the service.
- Installs and enables `prometheus-node-exporter` on every managed host **except k8s nodes** — those already run `node_exporter` as an in-cluster DaemonSet bound to `:9100`, so the Debian package's service would collide on the port; they set `baseline_node_exporter: false` and the package is removed there instead. Pointing Prometheus at new targets is a HelmCharts change, separate from this role.
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
- **Installs the homelab CA root certificate** from this role's `files/homelab-root.crt` into `/usr/local/share/ca-certificates/homelab-root.crt` and runs `update-ca-certificates -f` on every converge, so every managed host trusts step-ca-issued internal TLS leaves. The `-f` is load-bearing: Debian's ca-certificates package rebuilds `/etc/ssl/certs/ca-certificates.crt` on its own triggers (apt upgrades, openssl post-install hooks) and a previously-installed homelab root can quietly disappear from the bundle. The forced refresh re-asserts it idempotently. The same cert file is the artefact the `step-ca-bootstrap` runbook reads for CA rotation and verification. See `/work/AnsibleSpecs/slices/internal-tls-step-ca.md` §C.
- **Re-asserts `/etc/netplan/50-cloud-init.yaml`** when the host declares a `static_netplan` host_var. Mirrors the netplan that `terraform/prd/cloud-init.yaml.tftpl` writes on first boot, so a static-IP change in `vms.tf` plus a matching host_var update lands on the running host without a rebuild. Skipped when `static_netplan` is undefined. See `/work/AnsibleSpecs/decisions.md` "Cloud-init is a first-boot artefact".
- **Pairs `.home` routing with public DNS.** When any `network_devices[*].nameservers` is defined (the same shape that triggers the static-netplan public-DNS render), the role drops `/etc/systemd/resolved.conf.d/home-routing.conf` with `DNS=10.2.1.2 10.2.1.3` and `Domains=~home`. Per-link DNS (8.8.8.8/8.8.4.4) stays the default route; `*.home` matches the global routing scope and goes to the in-cluster dnsmasq LBs. No new flag — public DNS *is* (public upstreams) + (`~home` routing). Hosts without explicit `nameservers` (DHCP-from-dnsmasq boxes like `srviac`, `wrkdev*`) get neither and resolve `.home` through the LAN dnsmasq natively. A handler restarts `systemd-resolved`, which briefly drops the local stub resolver. See `/work/AnsibleSpecs/slices/completed/home-dns-routing.md`.

## Updates

Baseline does not configure or run package upgrades on its own. The dedicated `update.yml` playbook (forthcoming) owns `apt update`/`full-upgrade`/conditional reboot, with `serial: 1` + drain/uncordon for k8s and Ceph nodes once those phases land. Until then, `baseline_apt_dist_upgrade: true` is the operator's manual hatch — set it on a host (or via `-e`) to force a one-time dist-upgrade through baseline. See `/work/AnsibleSpecs/decisions.md` "OS updates".

## Depends on

`bootstrap` — `pvginkel` must exist before we can drop a `.vimrc` into their home.

## Not in scope

- UFW / firewalling — left at Ubuntu defaults.
- Samba / winbind / NetBIOS name resolution — dropped; DNS handles name resolution.
- Jenkins pipeline SSH key on `~pvginkel/.ssh/authorized_keys` — deferred to Phase 4 (only k8s nodes need it).
