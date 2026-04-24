# Roles

One role per concern. Each role is self-contained: `defaults/`, `vars/`, `tasks/`, `handlers/`, `templates/`, `files/`, `meta/`.

Planned roles (in rough build order):

1. `bootstrap` — make a fresh host Ansible-manageable: `ansible` user, SSH key, sudoers, Python.
2. `baseline` — OS hygiene: hostname, timezone, apt cache, unattended-upgrades, SSH hardening, UFW.
3. `microk8s` — install, configure, join a cluster node.
4. `microceph` — install, configure, join a cluster node.
5. `proxmox_host` — Proxmox PVE host config.
6. `openbao` — OpenBao VM bring-up with Azure auto-unseal.
7. `keycloak` — realm / client / user management against a running Keycloak.
