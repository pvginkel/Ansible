# Playbooks

Playbooks compose roles against host groups. **One playbook per
*operation class*, not per feature** — the roles are idempotent, so a
convergence run touches only what drifted, and a single feature is
scoped at run time with `--tags` / `--limit`, never with a new
playbook.

Run from the `ansible/` directory:

```sh
ansible-playbook playbooks/site.yml --check --diff
```

- `site.yml` — converge the non-cluster managed hosts.
- `site-k8s.yml` — converge the k8s clusters in place (`serial: 1`,
  no drain/reboot). The cluster counterpart to `site.yml`.
- `site-openbao.yml` — converge the OpenBao service VMs
  (`srvvault1..3`). The OpenBao counterpart to `site-k8s.yml`;
  `site.yml` excludes the `openbao` group.
- `site-ceph.yml` — converge the microceph storage layer. Today only
  `ceph_dev` (srvk8sdev, whose baseline + microk8s converge through
  `site-k8s.yml`); the prd Ceph fleet is not yet Ansible-managed.
- `update-k8s.yml` — OS patching for k8s nodes (drain → reboot).
- `rebuild-k8s.yml` / `evict-k8s.yml` — full VM rebuild, and the
  pre-rebuild drain run against the old node.
- `refresh-k8s-addons.yml` — post-snap-upgrade microk8s addon refresh.
- `refresh-calico-token.yml` — rolling restart of `calico-node`,
  capping pod uptime below Calico's token-refresh stall window;
  `IaC/Scheduled Calico Rollout` runs it weekly.
- `renew-host-certs.yml` / `renew-internal-tls.yml` — scheduled
  certificate renewal (SSH host certs; `internal_tls` X.509 leaves).
  Both are threshold-gated no-ops outside the renewal window, and
  `IaC/Scheduled Certs` runs both weekly.
- `openbao-backup-secret-id.yml` — delivers a `backup` AppRole
  secret_id that its caller minted to every srvvault, each proving it
  with a login first, and does nothing else. SecretRotator runs it when
  it rotates `backup` (the `openbao` role README, §Backup pipeline).
- `rotate-ansible-key.yml` — authorises one public half of the
  `ansible` user's SSH key on every host of the `ansible_key` group
  and logs in to each with its private half, or takes exactly that
  public half off every one. The always-up hosts are reached over
  SSH; the VMs that may be off (srvk8sdev, the scratch VMs) through
  the guest agent of their PVE node, where the caller says they run.
  SecretRotator's `ssh-key` kind runs it twice per rotation, adding
  the new key, then removing the old one; the playbook's header lists
  its extra vars. Not part of `site.yml`.
- `step-ca.yml` — writes step-ca's five Secrets on the prd cluster
  from the `step_ca` role's files, and restarts step-ca when their
  data changed. The single entry point for step-ca's material: cluster
  bring-up runs it before step-ca is first applied, and every
  procedure that changes the material runs it. No job runs it.
- `reissue-host-cert.yml` — recovery for a host whose SSH host
  certificate already lapsed; connects over a bootstrap channel that
  does not depend on it.
- `adopt.yml`, `grow-disks.yml` — one-off operations.
