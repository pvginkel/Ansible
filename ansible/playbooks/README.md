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
  `iac-scheduled-calico` runs it weekly.
- `renew-host-certs.yml` / `renew-internal-tls.yml` — scheduled
  certificate renewal (SSH host certs; `internal_tls` X.509 leaves).
  Both are threshold-gated no-ops outside the renewal window, and
  `iac-scheduled-certs` runs both weekly.
- `reissue-host-cert.yml` — recovery for a host whose SSH host
  certificate already lapsed; connects over a bootstrap channel that
  does not depend on it.
- `adopt.yml`, `grow-disks.yml` — one-off operations.
