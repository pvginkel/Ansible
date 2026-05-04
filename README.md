# Ansible — homelab infrastructure

Ansible + Terraform managing the Proxmox cluster, the microk8s + microceph VMs on top, and (partially) the Linux dev box. Helm continues to own workloads on Kubernetes; Jenkins continues to run deploys.

## Scope

- **Managed**: Proxmox hosts, k8s VMs + cluster, Ceph VMs + cluster, Linux dev box base setup.
- **Out of scope**: Home Assistant VMs, Windows VMs, end-user devices, IoT. UDM Pro + managed switch deferred.

See [`/work/AnsibleSpecs/decisions.md`](../AnsibleSpecs/decisions.md) for the full decision record — tooling, secrets, workflow. Plan + progress: [`/work/AnsibleSpecs/`](../AnsibleSpecs/) (separate repo) tracks phases (sequenced build-out) and slices (forward-looking design).

## Layout

```
.
├── ansible/                 # Ansible content
│   ├── ansible.cfg
│   ├── collections/         # ansible-galaxy collections
│   ├── inventories/
│   │   ├── prd/             # every production-grade host
│   │   └── scratch/         # disposable scratch fleet (Phase 4 microk8s scratch pair)
│   ├── playbooks/
│   ├── roles/
│   └── files/
├── terraform/               # VM provisioning (bpg/proxmox)
├── docs/runbooks/           # operational procedures (perpetual; design + plans live in /work/AnsibleSpecs)
├── pyproject.toml           # Poetry-managed Python deps
└── .pre-commit-config.yaml  # yamllint + ansible-lint on commit
```

## Prerequisites

One-time setup of the workstation that runs Terraform and Ansible is documented in [`docs/runbooks/operator-workstation.md`](docs/runbooks/operator-workstation.md): Python/Poetry, the two SSH identities (operator key for Terraform→PVE, `ansible` service key for playbook runs), DNS, and the Proxmox API token.

## Quickstart

```sh
poetry install
poetry run ansible-galaxy collection install -r ansible/collections/requirements.yml

cd ansible
poetry run ansible all -m ping      # once inventory is populated
```

Poetry creates an in-project `.venv/` (configured via `poetry.toml`). Prefix commands with `poetry run` or activate the venv explicitly (`source .venv/bin/activate`).

All Ansible commands run from the `ansible/` directory (where `ansible.cfg` lives).
