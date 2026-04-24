# Playbooks

Playbooks compose roles against host groups. One playbook per operational intent — `site.yml` covers everything, while smaller playbooks handle specific tasks (e.g. `k8s-upgrade.yml`, `ceph-upgrade.yml`).

Run from the `ansible/` directory:

```sh
ansible-playbook playbooks/site.yml --check --diff
```
