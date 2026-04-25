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

- DNS search domain is `.home`, configured on the operator workstation and (via the `baseline` role once written) on all managed hosts.
- All managed hosts **must** have forward DNS entries (`hostname.home`) resolvable from the operator workstation and from each other. Confirmed working today: `pve`, `pve1`, `pve2`.
- Ansible inventories use **short hostnames**; the `.home` search domain fills in the FQDN. Never hard-code IPs.
- For Terraform-provisioned VMs, the operator registers a DNS A record manually after `terraform apply`, until DNS-registration is automated. Automation is deferred; the DNS server's API capability still needs to be inventoried.

## MAC addressing for managed VMs

- VM NICs use deterministic MACs in the locally-administered range, computed from the Proxmox VMID. Pinned in Terraform so a rebuild keeps the same MAC.
- Format: `02:A7:F3:VV:VV:EE` — fixed locally-administered prefix `02:A7:F3`, then the VMID as two big-endian bytes (`VV:VV`), then the NIC index (`EE`). Example: VMID 900, NIC 0 → `02:A7:F3:03:84:00`.
- Constrains VMIDs to `[100, 65535]`. Validated at plan time by the `vm_id` variable.
- Future direction: a dnsmasq reservation resource (Terraform) keys IP + DNS off the MAC, so adding a VM becomes "register reservation, then provision." DHCP cutover for VMs comes with that work; today VMs still take static IPs via cloud-init.

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
