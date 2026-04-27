# Decision record

Source of truth for design decisions on this repo. When a decision changes, update this file — don't leave stale notes elsewhere.

## Scope

- **Ansible manages**: Proxmox hosts (100%), k8s VMs + cluster (100%), Ceph VMs + cluster (100%), Linux dev box base setup (partial — home-folder bits TBD).
- **Out of scope**: Home Assistant, Windows VMs, end-user devices, IoT.
- **Deferred**: UDM Pro + managed switch.
- Proxmox cluster is a real cluster (3 physical nodes, one PVE cluster).
- Ubuntu-only for Linux VMs.

## Design principles

Load-bearing rules. Specific decisions in this doc are consequences; if a principle changes, expect downstream decisions to need revisiting.

- **Terraform owns infrastructure state; Ansible owns OS/application state.** Disk geometry is Terraform; the filesystem on the disk is Ansible. VM existence is Terraform; packages on the VM are Ansible.
- **All roles are idempotent and safe to re-run.** Convergence is the model; drift is the trigger. Expensive work runs only on drift; a no-drift run is a fast no-op.
- **Cluster changes are serialized.** No parallel mutations of k8s or Ceph nodes. `serial: 1` plus drain/cordon hooks. Never two nodes at once.
- **The orchestrator cannot orchestrate its own replacement.** The Jenkins agent VM is mutated from the operator workstation, never from a pipeline running on itself. Self-reboot during orchestration is avoided.
- **Critical infrastructure sits outside the blast radius of what it depends on.** OpenBao does not run inside the k8s cluster that needs its secrets. The Jenkins agent does not run inside the k8s cluster it deploys to. Hosts that the dnsmasq pod depends on must not depend on it for DNS.

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
- **Recovery keys**: Shamir 3-of-5, stored in Roboform. Used only for admin ops (rekey, re-seal, new root token) — never during boot.
- **Wife runbook**: points at Roboform emergency access + recovery-key procedure. Lives in `docs/runbooks/`.
- **Future direction**: replace Azure auto-unseal with peer-unseal between two sites — a cheap USB-attached HSM at a friend's house unseals ours; ours unseals theirs. Removes the Azure dependency for routine reboots; manual Shamir is then only needed if both sites are simultaneously unreachable. Not pursued now; recorded as the option to revisit if Azure cost or trust becomes the constraint.

## OpenBao backup / DR

- Weekly automated JSON dump of KV secrets + policies + auth/mount config.
- Runs via systemd timer on the OpenBao VM; authenticates via an AppRole with read-only policy.
- Encrypted with `age` before leaving the box. Public key lives on the backup machine (no protection needed, it's public). Private key stored only in Roboform.
- Backup file written to an existing cloud-storage path (already daily-synced).
- **12-week retention**, older pruned via `rclone`.
- **Seal-migration runbook** (Azure → Shamir) documented in `docs/runbooks/`. Untested but written.
- **Three independent failure domains**: Azure (wrap key), Roboform (age private key + recovery shards), the box itself (ciphertext + age public key). Losing any two still allows recovery.

## Workflow + learning

- "Bob Ross" mode: Claude builds and annotates, user reads, reviews, and tweaks. No step-by-step hand-holding.
- Design artifacts live in this repo (`docs/`, READMEs). Not in Claude's memory.
- Throwaway VMs on Proxmox are used for learning. No sacrificial Proxmox host is available.
- The existing procedural runbook in `/work/Obsidian/` is the source material for role content. Ported topic-by-topic as roles are built.
- microk8s/microceph setup is "scripted textually" — scripts still need to be located on disk.

## Transitional cleanup tasks age out

When a role removes a thing that won't naturally come back — an orphaned authorized_keys entry, a UI tag from a now-defunct workflow, scratch scripts left over from troubleshooting — the cleanup task is a one-shot. Once it has converged on every host that needed it, the task is dead weight: it runs on every future apply, finds nothing to do, and adds noise to the role.

**Policy**: remove transitional cleanup tasks from roles after they've successfully converged. The convergence is the proof the cleanup did its job; what's gone won't come back without an external regression. Schedule the removal as a separate commit a couple of weeks after the cleanup landed so the soak window is visible in git history.

If the cleanup target *can* recur (drift the operator might re-introduce — a value the UI lets you set, a file someone might re-create), keep the task. The bar for keeping is "this could come back without an unrelated bug introducing it."

## Adoption is a waypoint; rebuild is the parity event

Hosts brought under management without being built from scratch will never be byte-for-byte identical to a from-scratch role apply. Retroactively reconciling them is a pipedream; **rebuild is the only parity mechanism that actually works**.

Adoption is therefore a transitional state. Every adopted VM has a planned rebuild that ends the transition. Trigger per host class:

| Host class  | Rebuild trigger          | Notes                                                                                                  |
|-------------|--------------------------|--------------------------------------------------------------------------------------------------------|
| k8s VMs     | As part of Phase 4       | microk8s state lives in `/var/snap/microk8s`; nothing OS-side worth preserving. `serial: 1` with drain/cordon. |
| Ceph VMs    | As part of Phase 5       | OSD disks reattached to fresh OS, not reformatted (see "Ceph rebuild path" below).                     |
| `wrkdev`    | Operator-scheduled       | Operator-managed; rebuild on operator's cadence.                                                       |
| pve hosts   | **No scheduled rebuild** | Bare metal, no shadow-clone, no destroy-and-recreate. Fidelity-only.                                   |

### Pre-rebuild sanity check (option)

For any rebuildable host, **file-based comparison against a shadow VM** is available as a per-host pre-rebuild check: spin up a from-scratch build via the role, rsync-diff against the live host with obvious exclusions (`/var`, `/proc`, `/sys`, machine-id, generated caches), inspect the residue. Not a routine workflow — the work only earns its keep right before a rebuild — but kept as a documented option because it's the only way to see exactly what was hand-modified outside the role.

### Fidelity for unrebuildable hosts (pve)

For the three pve hosts, fidelity to the role definitions is the only reachable goal. Mechanisms:

- `apt-mark showmanual` diff against the role's package list — highest signal.
- One-shot `/etc` snapshot (or `etckeeper`) → surfaces hand-edits since install.
- `systemctl list-unit-files --state=enabled` and `crontab -l` per user → catches scheduled side-channels.
- `ansible-playbook --check --diff` against the live host → anything `changed` is drift to either codify or consciously accept.

### Ceph rebuild path

Specifics deferred to Phase 5; the preferred shape is recorded here so the phase doc inherits the constraint:

1. **Upgrade first** — bring the cluster to its target microceph LTS channel via `snap refresh`, mons before OSDs, `serial: 1`. Soak under real workload for several days; confirm `HEALTH_OK` and that HelmCharts consumers are unaffected.
2. **Then rebuild** — drain a node, TF-replace the VM, apply baseline + microceph role, reattach the existing OSD disks (BlueStore OSDs carry their identity on-disk). Repeat one node at a time.
3. **Fallback if microceph won't adopt existing OSDs**: stand up a single-node temp cluster on `pve`'s spare `/dev/sda`, mirror data over (`rbd mirror` for RBD; rsync of a snapshot for CephFS), cut consumers over, rebuild the original nodes, migrate back, decommission the temp cluster, reclaim the spare.

Sequencing rationale: rebuild has no real rollback (once the rootfs is destroyed, you can't go back); upgrade does (`snap revert`). Doing the well-understood step first means the harder step starts from a verified baseline. If rebuild fails and we end up on the migration path, we're already on the target version — no double-handling.

### Ceph version policy

**LTS channels only.** Ceph is infrastructure the operator does not want to think about; chasing latest costs small surprises for negligible benefit on this workload. Track the current Ceph LTS, upgrade when the previous one goes EOL or sooner if a security fix forces it. Phase 5 picks the initial target channel against current state.

## Environment mapping

Ansible inventories and HelmCharts config folders use the same names (`prd`, `dev`). The split is **functional**, not a risk-tolerance gradient — both tiers are production-grade infrastructure where breakage hurts. Operational discipline (check-mode first, audit before rebuild, no sloppy applies) applies equally.

| Where              | `prd` means                                       | `dev` means                                        |
|--------------------|---------------------------------------------------|----------------------------------------------------|
| Ansible inventory  | Self-hosted production services: PVE cluster, prod k8s (3-node microk8s), Ceph cluster, OpenBao VM, dnsmasq, personal apps | Production-grade infrastructure whose workload is supporting development: single-node `wrkdevk8s`, operator workstation `wrkdev` |
| HelmCharts configs | Helm configs for the production cluster          | Helm configs used while developing/testing charts against the dev cluster |

`dev` does **not** mean "where you can break things." It means "production for development workflows." The only host where breakage is free is `wrkscratch`, which sits outside both inventories. When a procedure says "test it on the scratch VM first," that is `wrkscratch` — never `wrkdev` or `wrkdevk8s`.

The user's application has four deployment stages: `dev`, `test`, `uat`, `prd`. **All four run on the production Kubernetes cluster**, as separate namespaces. These stages are Helm's concern; Ansible does not see or manage them.

The HelmCharts `configs/dev` folder is **not** for app-dev instances — it is for iterating on Helm charts themselves against the single-node cluster. Do not confuse "dev the infra" with "dev the app stage."

## DNS and hostnames

- DNS search domain is `.home`. Configured on the operator workstation directly; pushed to every managed Ubuntu VM as DHCP option 15 by dnsmasq, so the `baseline` role does not have to set it.
- All managed hosts **must** have forward DNS entries (`hostname.home`) resolvable from the operator workstation and from each other.
- Ansible inventories use **short hostnames**; the `.home` search domain fills in the FQDN. Never hard-code IPs.
- For Terraform-provisioned VMs, the operator adds a dnsmasq reservation (MAC → IP, hostname) **before** `terraform apply`, so the VM's first DHCP request lands on the reserved address and the matching A record resolves. Terraformed reservations come later (see "MAC addressing" below).
- **Bootstrap-critical hosts do not resolve through the dnsmasq pod.** dnsmasq runs as a Kubernetes pod, so the k8s nodes themselves and the OpenBao VM cannot depend on it: the cluster could not boot from cold if its nodes resolved through a service hosted on the cluster, and OpenBao must be reachable to deliver secrets to the cluster that hosts dnsmasq. These hosts carry static resolver configuration — `/etc/hosts` for the names they need at boot, plus an upstream resolver (LAN router or public DNS) reached directly. The configuration is not standard Ubuntu defaults; the `baseline` role applies it based on host class.

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

## OS updates

Three policies, one per host class. The class is a property of the host, recorded in inventory.

| Class | Members | Update policy |
|---|---|---|
| **Cluster members** | k8s nodes, ceph nodes | Ansible-controlled. `update.yml` plays drain → `apt full-upgrade` → conditional reboot → uncordon, with `serial: 1`. Operator-triggered for now; CI-scheduled in Phase 10. |
| **Standalone VMs** | Jenkins agent VM, OpenBao VM | `unattended-upgrades` + auto-reboot in a quiet window. Ansible installs and configures the package, then steps back. |
| **Self-managed** | Home Assistant, Windows VMs, IoT, end-user devices | Outside this update system entirely. Documented but not managed. |

Why the split:
- The Jenkins agent cannot run a pipeline that reboots itself — the orchestrator must not be what is being mutated. Letting the OS handle its own updates avoids this.
- OpenBao with Azure auto-unseal re-engages its seal automatically on reboot, so it no longer needs the operator-triggered cadence the original "Ansible owns updates everywhere" rule assumed.

Concrete behaviour:
- **`baseline` enforces the package state per host class.** On cluster members it purges `unattended-upgrades` (no silent fallback to Ubuntu defaults). On standalone VMs it installs and configures it. Observable state is "package state matches host class."
- **Stop-gap until `update.yml` lands**: `baseline_apt_dist_upgrade: true` on a cluster-member host forces a dist-upgrade through baseline. Manual but adequate for a homelab on a private network.

Operational guards (to be folded into runbooks/playbooks at the relevant phase):
- **Stagger reboot windows.** The Jenkins agent VM and the OpenBao VM must not reboot in the same window. A simultaneous reboot would compound any unseal/connectivity issue and would mean nothing left running to diagnose it.
- **Post-boot health check on OpenBao.** After its reboot window, confirm OpenBao came back unsealed within N minutes; alert otherwise. Catches silent Azure-unseal failure (firewall drift, expired SP credential, Azure outage) early instead of when the next consumer fails to fetch a secret.

## Proxmox VM CPU affinity

- **`pve` core zoning**: cores `0-11` are reserved for interactive workloads (operator dev box, jump box, scratch VMs); cores `12-19` are for background workloads (Ceph, Kubernetes, Home Assistant). `pve1` and `pve2` are different machines and not zoned — affinity does not apply to VMs running there.
- **API constraint**: Proxmox restricts the VM `affinity` config field to `root@pam`. The scoped `terraform@pve!automation` token cannot set it under any role (confirmed: `HTTP 500 — only root can set 'affinity' config`). Granting the Terraform user root would broaden blast radius unacceptably.
- **Decision**: affinity is **reconciled by Ansible**, not Terraform. Terraform creates the VM; an Ansible task on the pve host runs `qm set <vmid> --affinity <range>` idempotently as root.
- **Source of truth**: each managed VM in inventory declares
  - `vm_id` — its Proxmox VMID,
  - `pve_node` — which physical PVE host runs it (`pve`, `pve1`, `pve2`); defaulted at the parent-group level to `pve`, overridden on the VMs that run elsewhere,
  - `workload_class` — `interactive` or `background`.

  The class → core-range map is per-PVE-host inventory data (only `pve` carries one, in `host_vars/pve.yml`). The `proxmox_host` role on `pve` enumerates VMs whose `pve_node` matches itself, resolves each to a core range via that map, and reconciles `qm set <vmid> --affinity <range>`. On `pve1`/`pve2` the role no-ops on affinity because no map is defined.
- **Lands in Phase 2** (`proxmox_host` role). Until that role exists, affinity is set by hand as root after `terraform apply`.

## Production execution model (Jenkins-driven)

How Terraform and Ansible run in production once Phase 10 lands. The operator workstation is reserved for changes that mutate the Jenkins agent VM itself; everything else flows through CI.

- **Dedicated Jenkins agent VM** for Terraform and Ansible runs. Not shared with other build workloads.
- **All logic lives in a Docker image.** The CI job pulls and runs the container on every execution; the agent VM holds no tool versions, no clone, no credentials cache. VM stays fully stateless and disposable.
- **tfstate is a local file inside a dedicated Git repo.** The container clones the state repo at job start, runs `terraform`, then commits and pushes any changes before exit. No remote-state backend (S3, Terraform Cloud, etc.).
- **Concurrency control at the Jenkins level.** A job-level lock prevents two TF/Ansible runs from racing — this is what makes the file-based state safe. No `terraform force-unlock` workflow because there is no remote lock to hold.

Path split — what runs where:

- **Through CI (the default path)**: every routine change. Bootstrap of new hosts, role applies, disk resize, OS updates on cluster nodes, scheduled drift checks. The Jenkins agent SSHes into the target host like any other Ansible run.
- **From the operator workstation (the carve-out)**: only changes that mutate the Jenkins agent VM itself — first-time bootstrap of the agent, agent VM disk resize, agent VM replace/destroy, break-glass when CI is down. The orchestrator cannot orchestrate its own replacement.

Guards against accidental self-mutation:

- **Terraform `lifecycle { prevent_destroy = true }`** on the Jenkins agent VM and the OpenBao VM resources. Hard stop at apply on a destroy.
- **CI plan-stage check** that fails the pipeline if `terraform plan` proposes `replace` or `destroy` on either VM. Belt-and-braces with `prevent_destroy` — the lifecycle block stops apply, the plan check stops the run before it ever reaches apply.

Implications:
- The state repo is a sensitive artifact (host private keys, API tokens). Same protections as any other secret-bearing repo.
- Rebuilding the agent VM is a no-op operationally; everything reproducible from the image + the state repo + Jenkins job config — but only via the workstation path.
- Image build and tagging are part of the CI surface — pin versions in the image, not on the VM.

## Backup

- **Cluster vzdump job** — Ansible-managed via the `proxmox_host` role from Phase 2. Daily snapshot-mode dump of every VM to the `local-backup` storage on `pve`, mail-on-failure to the operator, retain three. The job lives in `/etc/pve/jobs.cfg` (cluster-shared via pmxcfs); the role writes it from `pve` only and the cluster propagates.
- **Per-VM `backup` flag follows the node, not the VM**. A PVE host either has a backup datastore or it doesn't; today only `pve` does. Rule: every managed disk on a VM hosted on a backup-capable node is `backup=true`; everything else (VMs on `pve1`/`pve2`, plus all passthrough disks regardless of node) is `backup=false`. Passthrough disks (Ceph OSD volumes, ZFS-passthrough drives) are always `backup=false` because the stacks on top of them own redundancy and a vzdump of a multi-TB raw passthrough is neither crash-consistent nor cheap. Encoded as a per-node `pve_node_backup_datastore` attribute (Phase 3); read by the per-VM Terraform modules to set the `backup` flag on each disk.
- **Daily cloud sync across providers** — operator workflow, not Ansible. Untouched.
- **Git** — covers everything in this repo.
- **Offsite for production** is a later item.

Deferred / revisit:

- **Ansible-side assertion of the backup-flag policy.** Today the rule is enforced at Terraform time. A drift-detection step in `proxmox_host` could `qm config` each VM and flag any disk whose `backup=` does not match what its node attribute says. Worth folding into Phase 10's drift detection rather than building now — there is no second authoritative source today.
- **Wire the vzdump job's `node` to the same attribute.** `proxmox_host_backup_node` and `proxmox_host_backup_storage` are hardcoded in the role's defaults today. Both should be derived from `pve_node_backup_datastore` so adding a backup datastore to `pve1` (hypothetically) does not require a second edit. Mechanical change; not urgent.

## First-week plan

1. Repo skeleton committed — `ansible/`, `terraform/`, `docs/`, pre-commit with yamllint + ansible-lint, pinned tool versions.
2. SSH + passwordless-sudo sanity check from `wrkdev`.
3. Throwaway VM created via Terraform (exercise that path first).
4. `bootstrap` + `baseline` Ansible roles applied to the scratch VM.
5. Full inventory built out with all real hosts listed but none touched (only `--check` runs).
6. OpenBao stand-up deferred to week 2 — one new tool at a time.
