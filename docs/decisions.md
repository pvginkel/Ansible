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

- **Ansible** owns the platform up to "ready to host workloads": Proxmox host config, VM OS baseline, microk8s/microceph install + upgrade, cluster infrastructure on top of that (CNI mode + autodetect, MetalLB IP pool, kernel modules, addon enablement, registry mirror config), Ceph resources (RBD/CephFS), Keycloak realms/clients.
- **Helm** continues to own Kubernetes user workloads (application charts, per-environment configs). Not replacing it.
- **Jenkins** continues to run deploys. CI-triggered playbook runs will join.
- **Terraform** (with the `bpg/proxmox` provider) provisions VMs. Ansible takes over for configuration.
- Both Terraform and Ansible live in this repo.

**Terraform and Ansible are peer tools — neither invokes the other.** The operator runs each in sequence (and CI does likewise in Phase 10). A composite operation that needs both — drain via Ansible, `terraform apply -replace`, reattach + re-converge via Ansible — is documented in the relevant runbook as a sequence of operator commands, never a single playbook that shells out to `terraform`. Keeps responsibility boundaries clean: TF changes are auditable in `terraform plan`; Ansible changes are auditable in `--check --diff`; mixing them blurs both, and a TF failure halfway through an Ansible play is harder to recover from than a TF failure between two playbook invocations.

The line between Ansible and Helm is "what does the cluster need in order to be usable?" — that's Ansible. The CIDR config, Calico mode, MetalLB allocation, CoreDNS rewrites that resolve cluster-internal names (the registry alias) sit on the Ansible side because without them the cluster can't host anything. What an application carries with it — its own CoreDNS rewrites for app domains, IngressRoute, Helm-deployed pods, ExternalSecrets specs — sits on the Helm side.

The pre-Ansible `/work/KubernetesConfig` repo predates this split: it codified bring-up steps (`.microk8s.yaml`, MetalLB IPAddressPools, registry mirror config, procedural install docs) in a third location that today belongs on the Ansible side. Phase 4 absorbs its contents into the `microk8s` role and inventory; after Phase 4 lands, KubernetesConfig is archived. The operator runs their own ingress controller and own container registry from HelmCharts — `core/ingress` and `core/registry` are not enabled.

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

### k8s version policy

**LTS channels only.** Same logic as Ceph. Track the current microk8s LTS channel; upgrade when the previous goes EOL or sooner if a security fix forces it. Channel pinned per cluster in `group_vars/k8s_{prd,dev}.yml` so dev can soak a new minor independently before prd moves.

Today: `1.32/stable`, both clusters. The strict-confinement variant (`1.32-strict/stable`) is rejected — extra surface for limited benefit on this workload.

### k8s node capability labels

Per-node capability labels reconciled by the `microk8s` role from `host_vars/<node>.yml`. Today's set:

- `homelab.local/performance=high` — node has materially more CPU than peers (today: `srvk8s1`, 8 cores vs. 3 on the smalls). Workloads that want fast cores opt in via required nodeAffinity (Jenkins agent template, Plex).
- `homelab.local/storage=zpool2` — node has the ZFS passthrough disk surfaced as `zpool2` (today: `srvk8s1`). hostPath workloads (storage chart, Prometheus) opt in via required nodeAffinity. The pool name in the label leaves room for `storage=zpool3` if a second pool ever lands.

Labels are operator intent, not auto-derived from facts. The TF-side facts (`cpu_cores`, `passthrough_disks`) are inputs to a deliberate decision about which nodes earn which capability label; host_vars carries the label declarations alongside the other per-node inventory data with inline comments back to the TF source. Auto-derivation is rejected — bumping a small node from 3 cores to 6 should not silently retag it as a Plex target.

No taints. Affinity is opt-in: workloads that need a capability declare `requiredDuringSchedulingIgnoredDuringExecution`; everything else schedules freely. The legacy `size=large/small` labels and the `size=large:PreferNoSchedule` taint are removed during Phase 4.

### Dashboard tooling

Today: microk8s's `dashboard` addon (the upstream `kubernetes/dashboard` project bundled with the snap). The operator depends on the web UI day-to-day; codified into `microk8s_addons` for prd and dev.

**Revisit after the plan**: [Headlamp](https://headlamp.dev/) (CNCF, modern UI, plugin system) deployed as a Helm chart in `HelmCharts`. Same web-UI workflow, version ownership shifts off microk8s's release cadence onto the operator's Helm flow. `k9s` (terminal UI) and OpenLens / desktop apps are off the table — operator wants browser-based.

## Environment mapping

Two Ansible inventories: `prd` and `scratch`. The split is **production-grade vs deliberately disposable**, not a risk gradient.

- **`prd`** holds every host that must keep working: the PVE cluster, the 3-node prod k8s cluster (`k8s_prd`), the dev k8s node (`k8s_dev` — `wrkdevk8s`), the Ceph cluster, the OpenBao VM, the operator workstation (`wrkdev`). All production-grade. CI's default path runs against this inventory.
- **`scratch`** holds the disposable Terraform-provisioned scratch fleet — today, two microk8s scratch nodes (`wrkscratchk8s1`, `wrkscratchk8s2`) used in Phase 4 to exercise the role install + idempotent join paths. The only hosts where breakage is free.

When a procedure says "test it on a scratch VM first," that means a host in the `scratch` inventory — never `wrkdev` or `wrkdevk8s`. `wrkdev` is the operator's workstation; `wrkdevk8s` is the single-node cluster used to develop HelmCharts against.

HelmCharts uses its own `configs/dev` and `configs/prd` folders. That split is independent of Ansible's inventories: Helm's `configs/dev` is for iterating on Helm charts themselves against `wrkdevk8s`; `configs/prd` is for the production cluster. Don't conflate the two repos' uses of "dev."

The user's application has four deployment stages: `dev`, `test`, `uat`, `prd`. **All four run on the production Kubernetes cluster**, as separate namespaces. These stages are Helm's concern; Ansible does not see or manage them.

## DNS and hostnames

- DNS search domain is `.home`. Configured on the operator workstation directly; pushed to every managed Ubuntu VM as DHCP option 15 by dnsmasq, so the `baseline` role does not have to set it.
- All managed hosts **must** have forward DNS entries (`hostname.home`) resolvable from the operator workstation and from each other.
- Ansible inventories use **short hostnames**; the `.home` search domain fills in the FQDN. Never hard-code IPs.
- For Terraform-provisioned VMs, the per-VM module declares a `homelab_dns_reservation` resource that registers the (hostname, MAC) pair with the dnsmasq sidecar API; the API allocates the IPv4 from `10.1.3.0/24`. `depends_on` on the VM resource orders the reservation before VM create, so the VM's first DHCP request lands on a known reservation. See "MAC addressing" below for the resource shape.
- **Bootstrap-critical hosts do not resolve through the dnsmasq pod.** dnsmasq runs as a Kubernetes pod, so the k8s nodes themselves and the OpenBao VM cannot depend on it: the cluster could not boot from cold if its nodes resolved through a service hosted on the cluster, and OpenBao must be reachable to deliver secrets to the cluster that hosts dnsmasq. These hosts carry static resolver configuration — `/etc/hosts` for the names they need at boot, plus an upstream resolver (LAN router or public DNS) reached directly. The configuration is not standard Ubuntu defaults; the `baseline` role applies it based on host class.
- **The operator workstation needs a secondary resolver too**, for the same reason. The dnsmasq pod runs as a 2-replica StatefulSet pinned to different k8s nodes, so a single node reboot is invisible to it — but if the workstation only knows about one of the two replicas, a roll that touches the node hosting that replica blacks out resolution from the workstation mid-run. DHCP option 6 advertising both replicas covers it; configuring both resolvers statically on the workstation works too. Either way, list both — never one.

## Network topology for managed VMs

The Proxmox cluster has two physical bridges plus a workload VLAN on the first:

- **`vmbr0`** — 1 Gb house network. Internet-facing. Default route, DNS, DHCP (dnsmasq) all live here. Each managed VM's `network_devices[0]` lives on this bridge with `vlan_id=0`; the per-VM module's `homelab_dns_reservation` keys off that NIC's MAC.
- **`vmbr1`** — 10 Gb backplane between the PVE/Ceph/k8s nodes. Separate subnet, not reachable from the house LAN. Carries inter-node Ceph and Kubernetes traffic. Per-VM static address declared in `vms.tf` (or rendered guest-side at provision time); no IPAM, no reservation resource. Addresses are stable across rebuilds — the backplane is a shared subnet across PVE/Ceph/k8s/etc., so they're hand-curated.
- **`vmbr0` tag 2** — Kubernetes workload network. Same physical 1 Gb fabric as vmbr0, separate VLAN and subnet (`10.2.0.0/16`). Reserved for k8s services; BGP via MetalLB was partly set up and abandoned, but the subnet allocation is preserved. Per-VM static address declared in `vms.tf`, sequential within `10.2.0.0/16`. No IPAM.

Per-host-class shape:

| Class | NICs |
|---|---|
| Ceph nodes (`srvceph1/2/3`) | vmbr0 + vmbr1 |
| k8s nodes (`srvk8s1/2/3`) | vmbr0 + vmbr0 tag=2 + vmbr1 |
| Everything else (operator workstation, OpenBao, scratch) | vmbr0 only |

Deferred / revisit:

- **Audit that vmbr1 actually carries the traffic it's meant to.** The 10 Gb backplane was built up incrementally; the operator is not confident every Ceph/k8s node is steering traffic over it as designed. Verify once Phase 3a is done and Terraform is the source of truth for VM network config — the audit is much cheaper against a known-declarative baseline.

## VMID convention

- **Operator-created VMs (legacy)** keep their existing VMIDs in the 100–199 range. Today: `103` (srvk8sl1), `104` (srvk8ss1), `107` (srvk8ss2), `113` (srvceph1), `114` (srvceph2), `115` (srvceph3), plus the unmanaged VMs.
- **Terraform-owned VMs** use the **900-and-up range**. VMIDs `900–909` are reserved for the scratch fleet (today: `wrkscratchk8s1=901`, `wrkscratchk8s2=902`); `910` and up belong to the persistent fleet. The convention extends to every TF-managed VM going forward.
- Phase 3a imports the six existing managed VMs under their legacy VMIDs — no live mutation, just modeling what's there. Phase 4 (k8s) and Phase 5 (Ceph) rebuilds reassign them to VMIDs in the 900-and-up range as a side-effect of the rebuild. This also rotates each NIC to the deterministic-MAC scheme below (the locally-administered MAC is derived from the VMID), and prompts a one-time dnsmasq reservation update per VM.
- Phase 6's `srvvault` (OpenBao) and Phase 10's Jenkins agent VM are greenfield in the 900-and-up range from creation.

## MAC addressing for managed VMs

- **New / rebuilt VMs**: NICs use deterministic MACs in the locally-administered range, computed from the Proxmox VMID. Pinned in Terraform so a rebuild keeps the same MAC.
- Format: `02:A7:F3:VV:VV:EE` — fixed locally-administered prefix `02:A7:F3`, then the VMID as two big-endian bytes (`VV:VV`), then the NIC index (`EE`). Example: VMID 900, NIC 0 → `02:A7:F3:03:84:00`.
- Constrains VMIDs to `[100, 65535]`. Validated at plan time by the `vm_id` variable.
- VMs run DHCP on the NIC; cloud-init carries no IP/gateway/DNS config. dnsmasq is the single source of truth for IP and DNS, keyed off the pinned MAC.
- **Legacy (pre-rebuild) VMs**: keep their existing Proxmox-generated `BC:24:11:...` MACs pinned verbatim in their TF modules. The deterministic scheme applies after the Phase 4/5 rebuild, at which point the dnsmasq reservation is updated in lockstep with the new MAC.
- **dnsmasq reservation as a Terraform resource**: managed VMs register their (hostname, MAC) pair with the dnsmasq sidecar API via a `homelab_dns_reservation` resource inside each per-VM module; the API allocates the IPv4. One apply registers the reservation and creates the VM, in that order; destroy reverses it. The sidecar is Helm-deployed; the static `static-hosts.yaml` continues to hold operator-curated entries (printers, IoT, network gear) in a separate namespace, and is overridden by API entries on hostname collision. Specs: [`specs/dns-reservation-api.md`](specs/dns-reservation-api.md), [`specs/dns-reservation-terraform.md`](specs/dns-reservation-terraform.md).

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
- **Owner**: Terraform. The bpg/proxmox provider authenticates as `root@pam` (see `docs/runbooks/proxmox-credentials.md`), which lifts PVE's restriction on writing the `affinity` config field. The per-VM module's `cpu_affinity` input maps to `cpu.affinity` on the VM resource.
- **Source of truth**: the workload-class → core-range map lives in `terraform/prd/vms.tf` (mirrored in `terraform/scratch/vms.tf`). Each VM entry declares `workload_class = "interactive" | "background"`; the module call site computes `cpu_affinity = each.value.pve_node == "pve" ? local.workload_affinity_cores[each.value.workload_class] : null`. Pinning applies only on `pve`; VMs on `pve1`/`pve2` pass `null`.

## Terraform applies on cluster members never reboot directly

Generalization of "Cluster changes are serialized." A TF apply that triggers a VM reboot on a k8s or Ceph node disrupts workloads — no different from `apt-get install -y kernel-upgrade && reboot`. Cordon/drain (k8s) or `noout` + osd-down handling (Ceph) must precede the reboot, and that flow is owned by Ansible, not Terraform.

**Implementation**: the `terraform/modules/managed-vm/` child module sets `reboot_after_update = false` on the VM resource. Any config change applied through TF is written to PVE but does not take effect until the VM next reboots — and that reboot is operator-triggered through Ansible's update playbook (Phase 4/5), which performs the drain.

For changes that genuinely cannot wait — a BIOS mode flip, a CPU topology change — the path is: drain via Ansible → run TF apply → reboot via Ansible → uncordon. Never apply-then-reboot in one step on a live cluster member.

`reboot_after_update = false` applies to *all* managed VMs, not just cluster members — there is no harm in deferring reboots on standalone VMs either, and a uniform default keeps the module simple. Override per-VM only if the operator deliberately wants TF to reboot on apply.

## Disk passthrough on managed VMs

Passthrough disks are **first-class Terraform resources**. The per-VM module accepts a `passthrough_disks` input — a list of `{ interface, path_in_datastore }` — and declares them as additional `disk` blocks alongside the managed disks. TF creates and attaches them in the same apply as the VM, using the `root@pam` provider auth. There is no staged TF-then-Ansible flow.

Backups are always `backup = false` on passthrough blocks: the stacks on top (Ceph BlueStore on the OSD volumes, ZFS on the NVMe) own redundancy, and a vzdump of a multi-TB raw passthrough is neither crash-consistent nor cheap.

The disk identity (the `/dev/disk/by-id/<serial>` path) lives in the VM's `terraform/prd/vms.tf` entry. When a physical disk is swapped, edit that path and run a targeted `terraform refresh` + `terraform apply` — see `docs/runbooks/vm-rebuild.md`.

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
