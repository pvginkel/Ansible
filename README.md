# Ansible — homelab infrastructure

Ansible + Terraform managing the Proxmox cluster, the microk8s + microceph VMs on top, and (partially) the Linux dev box. Helm continues to own workloads on Kubernetes; Jenkins continues to run deploys.

## Scope

- **Managed**: Proxmox hosts, k8s VMs + cluster, Ceph VMs + cluster, Linux dev box base setup.
- **Out of scope**: Home Assistant VMs, Windows VMs, end-user devices, IoT. UDM Pro + managed switch deferred.

See [`/work/AnsibleSpecs/decisions.md`](../AnsibleSpecs/decisions.md) for the full decision record — tooling, secrets, workflow. Plan + progress: [`/work/AnsibleSpecs/`](../AnsibleSpecs/) (separate repo) tracks phases (sequenced build-out) and slices (forward-looking design).

For the physical layer — what the hardware actually is, how it is wired, and what the estate can and cannot survive — see [`docs/homelab-handover.md`](docs/homelab-handover.md). It is the only written record of the hardware; nothing regenerates it.

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
├── docs/homelab-handover.md # physical hardware + platform description, and the estate's failure characteristics
├── docs/runbooks/           # operational procedures (perpetual; design + plans live in /work/AnsibleSpecs)
├── support/iac-agent/       # srviac host glue (the `iac` shim, iac-impl, installer, systemd unit)
├── support/iac-image/       # Dockerfile for the `iac` image the Jenkins pipelines run in
├── pyproject.toml           # Poetry-managed Python deps
└── .kubecoder/              # KubeCoder environment shape + curated build/lint entry points
```

Linting is manual — there is no pre-commit hook. Run `kc project lint` before committing.

## Prerequisites

One-time setup of the workstation that runs Terraform and Ansible is documented in [`docs/runbooks/operator-workstation.md`](docs/runbooks/operator-workstation.md): Python/Poetry, the two SSH identities (operator key for Terraform→PVE, `ansible` service key for playbook runs), DNS, and the Proxmox API token.

## Quickstart

In a KubeCoder environment, the toolchain lives in the `iac` sidecar and setup is a single curated command:

```sh
kc project setup                    # poetry install + ansible-galaxy collections
kc project lint                     # yamllint + ansible-lint + terraform fmt
```

Ad-hoc commands reach the sidecar with `cexec`:

```sh
cd ansible
cexec iac poetry run ansible all -m ping    # once inventory is populated
```

All Ansible commands run from the `ansible/` directory (where `ansible.cfg` lives). On a workstation without KubeCoder, drop the `cexec iac` prefix and run `poetry install` yourself — see [`docs/runbooks/operator-workstation.md`](docs/runbooks/operator-workstation.md).
