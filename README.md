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
├── requirements.txt         # pinned Python deps (ansible, lint tools)
└── .pre-commit-config.yaml  # yamllint + ansible-lint on commit
```

## Prerequisites

- Python 3.11+
- `python3-venv` and `pip`
- SSH key to target hosts, passwordless sudo on targets

## Quickstart

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cd ansible
ansible-galaxy collection install -r collections/requirements.yml
ansible -i inventories/prd all -m ping      # once inventory is populated
```

All Ansible commands run from the `ansible/` directory (where `ansible.cfg` lives).
