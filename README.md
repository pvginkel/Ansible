# Ansible — homelab infrastructure

Ansible + Terraform managing the Proxmox cluster, the microk8s + microceph VMs on top, and (partially) the Linux dev box. Helm continues to own workloads on Kubernetes; Jenkins continues to run deploys.

## Scope

- **Managed**: Proxmox hosts, k8s VMs + cluster, Ceph VMs + cluster, Linux dev box base setup.
- **Out of scope**: Home Assistant VMs, Windows VMs, end-user devices, IoT. UDM Pro + managed switch deferred.

See [`docs/decisions.md`](docs/decisions.md) for the full decision record — tooling, secrets, workflow.

## Layout

```
.
├── ansible/                 # Ansible content
│   ├── ansible.cfg
│   ├── collections/         # ansible-galaxy collections
│   ├── inventories/
│   │   ├── prd/             # production hosts
│   │   └── dev/             # dev / workstation hosts
│   ├── playbooks/
│   ├── roles/
│   └── files/
├── terraform/               # VM provisioning (bpg/proxmox)
├── docs/                    # design + runbooks
├── pyproject.toml           # Poetry-managed Python deps
└── .pre-commit-config.yaml  # yamllint + ansible-lint on commit
```

## Prerequisites

- Python 3.12+
- [Poetry](https://python-poetry.org/) 2.x
- SSH key to target hosts, passwordless sudo on targets

## Quickstart

```sh
poetry install
poetry run ansible-galaxy collection install -r ansible/collections/requirements.yml

cd ansible
poetry run ansible all -m ping      # once inventory is populated
```

Poetry creates an in-project `.venv/` (configured via `poetry.toml`). Prefix commands with `poetry run` or activate the venv explicitly (`source .venv/bin/activate`).

All Ansible commands run from the `ansible/` directory (where `ansible.cfg` lives).
