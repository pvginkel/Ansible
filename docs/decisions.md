# Decision record

Source of truth for design decisions on this repo. When a decision changes, update this file — don't leave stale notes elsewhere.

## Scope

- **Ansible manages**: Proxmox hosts (100%), k8s VMs + cluster (100%), Ceph VMs + cluster (100%), Linux dev box base setup (partial — home-folder bits TBD).
- **Out of scope**: Home Assistant, Windows VMs, end-user devices, IoT.
- **Deferred**: UDM Pro + managed switch.
- Proxmox cluster is a real cluster (3 physical nodes, one PVE cluster).
- Ubuntu-only for Linux VMs.

## Tool split

- **Ansible** owns the platform: Proxmox host config, VM OS baseline, microk8s/microceph install + upgrade, Ceph resources (RBD/CephFS), Keycloak realms/clients.
- **Helm** continues to own Kubernetes workloads. Not replacing it.
- **Jenkins** continues to run deploys. CI-triggered playbook runs will join.
- **Terraform** (with the `bpg/proxmox` provider) provisions VMs. Ansible takes over for configuration.
- Both Terraform and Ansible live in this repo.

## Secrets — OpenBao

- **OpenBao**, not HashiCorp Vault proper. Linux Foundation fork, MPL 2.0, API-compatible with Vault. All Vault integrations work unchanged: `community.hashi_vault` (Ansible), External Secrets Operator (Helm), HashiCorp Vault Jenkins plugin.
- Runs in a **dedicated VM on Proxmox** — not in Kubernetes, to avoid the chicken-and-egg where k8s needs secrets that live in k8s.
- **Auto-unseal via Azure Key Vault** (Standard tier, software-protected RSA key). Estimated cost: ~$1/year.
- **Key Vault firewall** allowlists home WAN IP only. A stolen box moved off-network cannot reach Azure to unseal.
- **Dedicated Azure service principal** with minimum perms (`Get`, `Wrap Key`, `Unwrap Key`) on the one key.
- **Recovery keys**: Shamir 3-of-5, stored in Bitwarden. Used only for admin ops (rekey, re-seal, new root token) — never during boot.
- **Wife runbook**: points at Bitwarden emergency access + recovery-key procedure. Lives in `docs/runbooks/`.

## OpenBao backup / DR

- Weekly automated JSON dump of KV secrets + policies + auth/mount config.
- Runs via systemd timer on the OpenBao VM; authenticates via an AppRole with read-only policy.
- Encrypted with `age` before leaving the box. Public key lives on the backup machine (no protection needed, it's public). Private key stored only in Bitwarden.
- Backup file written to an existing cloud-storage path (already daily-synced).
- **12-week retention**, older pruned via `rclone`.
- **Seal-migration runbook** (Azure → Shamir) documented in `docs/runbooks/`. Untested but written.
- **Three independent failure domains**: Azure (wrap key), Bitwarden (age private key + recovery shards), the box itself (ciphertext + age public key). Losing any two still allows recovery.

## Workflow + learning

- "Bob Ross" mode: Claude builds and annotates, user reads, reviews, and tweaks. No step-by-step hand-holding.
- Design artifacts live in this repo (`docs/`, READMEs). Not in Claude's memory.
- Throwaway VMs on Proxmox are used for learning. No sacrificial Proxmox host is available.
- The existing procedural runbook in `/work/Obsidian/` is the source material for role content. Ported topic-by-topic as roles are built.
- microk8s/microceph setup is "scripted textually" — scripts still need to be located on disk.

## Environment mapping

Ansible inventories and HelmCharts config folders use the same names (`prd`, `dev`) but refer to *infrastructure environments*, not application deployment stages.

| Where              | `prd` means                                       | `dev` means                                        |
|--------------------|---------------------------------------------------|----------------------------------------------------|
| Ansible inventory  | Production infrastructure: PVE cluster, prod k8s (3-node microk8s), Ceph cluster, OpenBao VM | Chart-development single-node k8s cluster (`wrkdevk8s`) + Linux operator workstation (`wrkdev`) |
| HelmCharts configs | Helm configs for the production cluster          | Helm configs used while developing/testing charts against the dev cluster |

The user's application has four deployment stages: `dev`, `test`, `uat`, `prd`. **All four run on the production Kubernetes cluster**, as separate namespaces. These stages are Helm's concern; Ansible does not see or manage them.

The HelmCharts `configs/dev` folder is **not** for app-dev instances — it is for iterating on Helm charts themselves against the single-node cluster. Do not confuse "dev the infra" with "dev the app stage."

## DNS and hostnames

- DNS search domain is `.home`. Configured on the operator workstation directly; pushed to every managed Ubuntu VM as DHCP option 15 by dnsmasq, so the `baseline` role does not have to set it.
- All managed hosts **must** have forward DNS entries (`hostname.home`) resolvable from the operator workstation and from each other.
- Ansible inventories use **short hostnames**; the `.home` search domain fills in the FQDN. Never hard-code IPs.
- For Terraform-provisioned VMs, the operator adds a dnsmasq reservation (MAC → IP, hostname) **before** `terraform apply`, so the VM's first DHCP request lands on the reserved address and the matching A record resolves. Terraformed reservations come later (see "MAC addressing" below).

## MAC addressing for managed VMs

- VM NICs use deterministic MACs in the locally-administered range, computed from the Proxmox VMID. Pinned in Terraform so a rebuild keeps the same MAC.
- Format: `02:A7:F3:VV:VV:EE` — fixed locally-administered prefix `02:A7:F3`, then the VMID as two big-endian bytes (`VV:VV`), then the NIC index (`EE`). Example: VMID 900, NIC 0 → `02:A7:F3:03:84:00`.
- Constrains VMIDs to `[100, 65535]`. Validated at plan time by the `vm_id` variable.
- VMs run DHCP on the NIC; cloud-init carries no IP/gateway/DNS config. dnsmasq is the single source of truth for IP and DNS, keyed off the pinned MAC.
- Future direction: a Terraform resource for the dnsmasq reservation, so adding a VM becomes "register reservation, then provision" in one apply. Until then, the reservation is added by hand before `terraform apply`.

## SSH host keys for managed VMs

- Terraform generates an ed25519 host keypair per VM (`tls_private_key`), embeds the private half in cloud-init's `ssh_keys:` block so the VM boots with that identity, and writes the public half to `ansible/files/known_hosts.d/<config-name>` as a `known_hosts` entry. The repo is the registry; one file per Terraform config.
- Ansible's `ssh_args` set `UserKnownHostsFile` to the per-config file and `GlobalKnownHostsFile=/dev/null`, so playbook runs are independent of the operator's personal `~/.ssh/known_hosts` (and identical between workstation and ephemeral CI container). `HostKeyAlgorithms=ssh-ed25519` ignores the rsa/ecdsa keys sshd auto-generates non-deterministically.
- Public host keys are not secret. They're committed; the diff is auditable. Host private keys live in tfstate (already sensitive — the API token, cloud-init user-data containing our authorized SSH pubkeys, etc., were already there).
- A rebuild without `terraform taint tls_private_key.host_*` keeps the same identity, so destroy+recreate of a VM no longer trips host-key warnings. Cloud-init only runs on first boot, though, so picking up a new pinned key requires `terraform apply -replace=<vm-resource>`.
- **Future evolution**: once OpenBao is up (Phase 6+), replace the per-host pubkeys with one `@cert-authority *.home <CA pubkey>` line, signing host certs at provision time. Per-host files in `known_hosts.d/` collapse to one CA line.

## Proxmox VM CPU affinity

- **`pve` core zoning**: cores `0-11` are reserved for interactive workloads (operator dev box, jump box, scratch VMs); cores `12-19` are for background workloads (Ceph, Kubernetes, Home Assistant). `pve1` and `pve2` are different machines and not zoned this way — affinity does not apply to VMs running there.
- **API constraint**: Proxmox restricts the VM `affinity` config field to `root@pam`. The scoped `terraform@pve!automation` token cannot set it under any role (confirmed: `HTTP 500 — only root can set 'affinity' config`). Granting the Terraform user root would broaden blast radius unacceptably.
- **Decision**: affinity is **reconciled by Ansible**, not Terraform. Terraform creates the VM; an Ansible task on the pve host runs `qm set <vmid> --affinity <range>` idempotently as root.
- **Source of truth**: per-host VM-affinity map in Ansible inventory (e.g. `host_vars/pve.yml` → `proxmox_vm_affinity: { <vmid>: "<range>" }`). Only `pve`'s host_vars carry this; `pve1`/`pve2` don't.
- **Lands in Phase 2** (`proxmox_host` role). Until that role exists, affinity is set by hand as root after `terraform apply`.

## Existing backup context (not in Ansible scope)

- PVE VM snapshots, 3-day retention.
- Daily cloud sync across providers.
- Git.
- Offsite for production is a later item, not now.

## First-week plan

1. Repo skeleton committed — `ansible/`, `terraform/`, `docs/`, pre-commit with yamllint + ansible-lint, pinned tool versions.
2. SSH + passwordless-sudo sanity check from `wrkdev`.
3. Throwaway VM created via Terraform (exercise that path first).
4. `bootstrap` + `baseline` Ansible roles applied to the scratch VM.
5. Full inventory built out with all real hosts listed but none touched (only `--check` runs).
6. OpenBao stand-up deferred to week 2 — one new tool at a time.
