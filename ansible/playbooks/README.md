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
- `update-k8s.yml` — OS patching for k8s nodes (drain → reboot).
- `rebuild-k8s.yml` / `evict-k8s.yml` — full VM rebuild, and the
  pre-rebuild drain run against the old node.
- `refresh-k8s-addons.yml` — post-snap-upgrade microk8s addon refresh.
- `adopt.yml`, `grow-disks.yml` — one-off operations.
