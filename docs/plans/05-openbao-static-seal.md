# 05 — OpenBao: drop Azure, 3-node Raft, static seal, VIP via Keepalived

## Goal

Rewrite the OpenBao decisions before Phase 6 starts. Three changes
land together:

1. Drop Azure Key Vault auto-unseal in favour of a file-based static
   seal, distributed via ansible-vault.
2. Move from a single VM to a 3-node OpenBao cluster (one per PVE
   host) using OpenBao's integrated Raft. PVE-level HA is unreachable
   without ZFS or shared storage; application-layer HA is what every
   other cluster on this fleet (k8s, Ceph) already does.
3. Front the cluster with a leader-tracking VIP managed by Keepalived
   so failover is automatic, sub-5-second, and clients use a single
   `https://openbao.home:8200` endpoint that follows the Raft leader.

This is a doc-only plan. Phase 6 is still unwritten; this rewrites the
decisions that Phase 6's role + runbook + Terraform work will then
implement.

## Decisions taken with the operator

- **Application-layer HA over PVE-level HA.** ZFS-based PVE storage
  replication declined; shared storage (Ceph/NFS/iSCSI) ruled out.
  OpenBao's integrated Raft handles replication and leader election
  across three independent VMs, the same shape as the existing k8s
  and Ceph clusters.
- **3 VMs, one per PVE node**: `srvvault1` on `pve`, `srvvault2` on
  `pve1`, `srvvault3` on `pve2`. VMIDs `910`, `911`, `912` (persistent
  fleet, per the 900-and-up convention). Memory budget ~1 GB per VM
  is well within each PVE host's headroom; the PVE memory pressure
  the operator has seen in practice is ZFS ARC, not VM allocations.
- **Static seal key** in `/etc/openbao/seal/static.key` (root:openbao
  0440). Same file on all three nodes. Auto-unseal on boot, no cloud,
  no service principal. Distributed via **ansible-vault**: encrypted
  file in the repo, passphrase in Roboform.
- **VIP via Keepalived** with a `vrrp_script` health check that
  succeeds only on the current Raft leader. Leader's VRRP priority
  climbs to 100; followers stay at 50; VIP lands on the leader. Sub-
  5-second failover on a leadership change. No HAProxy, no nginx,
  no manual DNS flipping.
- **JSON dump on all three nodes, leader-only execution.** Each VM's
  systemd timer fires weekly with a randomized 15-minute delay; the
  guard `is_self == true` against `/v1/sys/leader` short-circuits on
  the two followers. One backup per cycle, lands in the existing
  daily-synced cloud-storage path.
- **Network boundary stays as ufw on each VM.** No management VLAN.
  vmbr0 only.
- **All three srvvaultN excluded from the cluster vzdump job.** The
  seal key and Raft data live on the same rootfs; bundling them in a
  PVE backup would defeat the seal. Same hygiene rule as the previous
  single-VM plan, applied across three.
- **Recovery model**:
  - Single VM loss (PVE node down, disk corruption, etc.): Terraform
    recreates the VM, role configures it, OpenBao Raft-joins the
    surviving cluster, leader streams the snapshot. ~5–10 minutes,
    fully automated, no operator decision.
  - Whole-cluster loss (extreme): Terraform recreates all three, role
    bootstraps fresh, JSON dump replays via API. The previous plan's
    recovery path, kept as the fallback.
- **Failure domains stay at two** (Roboform + the cluster) versus the
  three under the Azure design. Accepted as the cost of removing the
  cloud dependency. The HA work narrows the *availability* gap; it
  doesn't change the confidentiality gap.
- **TLS**: per-node cert with SANs covering own short hostname,
  FQDN, and the VIP hostname (`openbao.home`). Single cert per node;
  three near-identical certs across the cluster. Homelab CA approach
  (external one-shot vs. self-signed-with-explicit-trust vs. PKI-on-
  OpenBao itself) decided in the Phase 6 role design.

## Steps

### `docs/decisions.md` — "Secrets — OpenBao"

Rewrite the section. New content:

- OpenBao, not HashiCorp Vault proper. Linux Foundation fork,
  MPL 2.0, API-compatible with Vault. All Vault integrations work
  unchanged (`community.hashi_vault`, External Secrets Operator,
  HashiCorp Vault Jenkins plugin). *(unchanged)*
- Runs as a **3-node cluster** of dedicated VMs on Proxmox —
  `srvvault1` / `srvvault2` / `srvvault3`, one per PVE host. Not in
  Kubernetes, to avoid the chicken-and-egg where k8s needs secrets
  that live in k8s. **Integrated Raft** for replication and leader
  election; same application-layer HA shape as the existing k8s and
  Ceph clusters. PVE-level HA was rejected — ZFS replication needs
  ZFS on each node, and shared storage either reintroduces the
  Ceph chicken-and-egg or requires hardware investment.
- **Static seal** with the key at `/etc/openbao/seal/static.key`
  (root:openbao 0440), the same file on all three nodes. Auto-unseal
  on every boot from local disk. Replaces the previous Azure Key
  Vault auto-unseal — no cloud dependency, no service principal, no
  Azure firewall to maintain. The seal key is distributed via
  **ansible-vault** (encrypted file in the repo, passphrase in
  Roboform); a fresh node receives the key from `ansible-playbook
  --ask-vault-pass` on first apply.
- **Recovery keys**: Shamir 3-of-5, stored in Roboform. Used only for
  admin ops (rekey, re-seal, new root token) — never during boot.
  *(unchanged)*
- **Endpoint**: clients hit `https://openbao.home:8200`, a VIP
  managed by Keepalived on all three nodes. A `vrrp_script` polls
  `/v1/sys/leader` every two seconds; only the current Raft leader's
  check succeeds, raising its VRRP priority above the followers and
  pulling the VIP to it. Failover on a leadership change is bounded
  by `interval × fall + advert_int`, typically ~4–6 s. No HAProxy,
  no nginx, no manual DNS flips.
- **Network boundary**: ufw on each srvvaultN. Default-deny inbound.
  Allow `8200/tcp` from k8s node IPs and from the Jenkins agent VM;
  `8201/tcp` (Raft cluster traffic) from the other two srvvaultN;
  protocol VRRP (IP proto 112) from the other two srvvaultN; `22/tcp`
  from the Jenkins agent VM only. No management VLAN; vmbr0 only.
- **Admin path**: operator reaches OpenBao via VSCode Remote-SSH from
  wrkdevwin into the Jenkins agent VM, then `bao` / port-forwarded
  UI from there. wrkdevwin holds the personal SSH key for that one
  hop; the Jenkins agent VM holds the OpenBao admin token, the
  automation SSH keys for the fleet, and any other privileged
  material.
- **Wife runbook**: points at Roboform emergency access + recovery-
  key procedure. Lives in `docs/runbooks/`. *(unchanged; written as
  part of Phase 6.)*
- **Future direction**: peer-unseal between two sites (cheap USB HSM
  at a friend's house unsealing ours; ours unsealing theirs) remains
  available — it would restore a three-domain isolation model.
  Lower priority now that Azure is out of the picture.

### `docs/decisions.md` — "OpenBao backup / DR"

Rewrite the section. New content:

- **Canonical backup**: weekly age-encrypted JSON dump of KV secrets
  + policies + auth/mount config. *(unchanged)*
- **Runs on all three nodes; leader-only execution.** Each VM has a
  systemd timer that fires the same dump script with a randomized
  15-minute delay; the script guards on `/v1/sys/leader`'s `is_self`
  flag, so the two followers exit in milliseconds and only the
  leader writes. If a leadership election is in flight when the
  timers happen to fire, all three skip — the next cycle picks it up.
- Encrypted with `age` before leaving the box. Public key on each
  srvvaultN (no protection needed); private key in Roboform.
  *(unchanged)*
- Backup file written to an existing cloud-storage path (already
  daily-synced). 12-week retention pruned via `rclone`. *(unchanged)*
- **All three srvvaultN are excluded from the cluster vzdump job.**
  Seal key + Raft data co-located on the rootfs; bundling them in a
  PVE backup would defeat the seal. The JSON-dump path is the only
  backup; this also forces drills to exercise the path that actually
  matters.
- **Recovery paths**:
  - *Single-node loss* (PVE host down, VM corruption, disk failure):
    1. `terraform apply` recreates the affected `srvvaultN` on its
       `pve_node` (or, for an unrecoverable host, on a different
       one — the per-VM module places the VM, not OpenBao).
    2. `bootstrap` + `baseline` + `openbao` roles converge.
    3. Static seal key arrives via ansible-vault.
    4. The role's join task points the new node at the existing
       cluster; OpenBao Raft pulls the snapshot from the leader.
       VIP is unaffected throughout — it lives on whichever surviving
       node is leader.
  - *Whole-cluster loss* (all three down simultaneously, the extreme
    case):
    1. `terraform apply` recreates all three VMs.
    2. Roles converge; first node initializes a fresh OpenBao with
       the same seal key, others Raft-join.
    3. JSON dump (downloaded from cloud-storage, decrypted with the
       age private key from Roboform) is replayed via the API.
    4. ESO resyncs Kubernetes Secrets; consumers reconnect.
- **Failure domains**: two — Roboform (Shamir recovery keys + age
  private key + ansible-vault passphrase) and the cluster itself
  (Raft data + live seal key + age public key + the cloud-storage
  path with the encrypted dump). Both must leak for full secret
  compromise. Three-domain isolation existed under the Azure design;
  one domain was deliberately given up to remove the cloud
  dependency.
- **Recovery drill** is a Phase 6 deliverable: exercise the
  single-node-loss path on the live cluster (rebuild one VM, watch
  Raft snapshot); exercise the whole-cluster path on a scratch VM
  (init from JSON dump). Document timings in the runbook.
- The "seal-migration runbook (Azure → Shamir)" item is dropped;
  with no Azure in the design, there is no migration to document.

### `docs/decisions.md` — "OS updates"

Update the OpenBao paragraphs:

- "Standalone VMs" row in the policy table → rename to **standalone
  service VMs**, list members as "Jenkins agent VM, srvvault1/2/3"
  (preserve the existing `unattended-upgrades` policy; it still fits).
- "Why the split", second bullet:
  "OpenBao with Azure auto-unseal re-engages its seal automatically
  on reboot, so it no longer needs the operator-triggered cadence the
  original 'Ansible owns updates everywhere' rule assumed." →
  "OpenBao with static seal re-engages its seal automatically on
  reboot (key read from local disk), so it no longer needs the
  operator-triggered cadence the original 'Ansible owns updates
  everywhere' rule assumed. With three nodes in a Raft cluster, the
  cadence also matters less — quorum survives one node rebooting."
- "Operational guards":
  - The "stagger reboot windows" guard for Jenkins agent + OpenBao
    becomes "**stagger reboot windows across the four standalone
    service VMs.** Jenkins agent + srvvault1/2/3 must not reboot in
    the same window. For the OpenBao cluster specifically, no two
    srvvaultN should be in the same window — quorum survives one
    node rebooting, not two."
  - The post-boot health check guard:
    "Catches silent Azure-unseal failure (firewall drift, expired SP
    credential, Azure outage) early instead of when the next consumer
    fails to fetch a secret." → "Catches silent static-seal failure
    (key file missing, wrong permissions, service did not start, disk
    corruption) and rejoin failure (Raft can't reach a peer, TLS
    expired) early instead of when the next consumer fails to fetch
    a secret."

### `docs/decisions.md` — "VMID convention"

Update the Phase 6 reference:

- "Phase 6's `srvvault` (OpenBao) and Phase 10's Jenkins agent VM are
  greenfield in the 900-and-up range from creation." →
  "Phase 6's `srvvault1`/`srvvault2`/`srvvault3` (OpenBao cluster,
  VMIDs 910–912) and Phase 10's Jenkins agent VM are greenfield in
  the 900-and-up range from creation."

### `ansible/inventories/prd/hosts.yml`

Replace the singular forward declaration with three:

- Under `openbao:`, replace `srvvault:` with `srvvault1:`,
  `srvvault2:`, `srvvault3:`.
- Update the comment from "OpenBao VM, created in Phase 6." to
  "OpenBao 3-node Raft cluster, created in Phase 6."
- Drop the `# `openbao` joins once srvvault has a vm_id (Phase 6).`
  comment under `pve_vms:`. Membership stays deferred to Phase 6 (the
  host_vars don't exist yet, so adding the group now would surface
  empty-vm_id errors in the proxmox_host role); this plan only
  reshapes the names.

### `docs/plan.md` — Phase 6 summary

Rewrite the Phase 6 paragraph:

> Stand up the 3-node OpenBao Raft cluster (`srvvault1` / `srvvault2`
> / `srvvault3`, one per PVE host). Static seal with the key
> distributed via ansible-vault. Keepalived on each node fronts the
> cluster with a leader-tracking VIP at `openbao.home`. Canonical
> backup is the weekly age-encrypted JSON dump, run on all three
> nodes with leader-only execution; srvvaultN are excluded from the
> cluster vzdump job. AppRole credentials for Ansible, Jenkins,
> External Secrets Operator. ufw on each VM as the network boundary;
> Jenkins agent VM is the only ssh entry point. Per-consumer
> policies; root token retired immediately after bootstrap. Audit
> logging and systemd hardening from day one. Recovery drills
> (single-node and whole-cluster) executed before the phase closes.
> Migrate a first set of HelmCharts secrets to validate the path.

## Verification

Doc-only plan; no apply runs. Verification is operator review:

- Read the rewritten `docs/decisions.md` sections end-to-end and
  confirm no leftover Azure references (the original section had
  several entries; check each was rewritten or removed).
- Confirm the failure-domain trade-off is captured plainly (we lost
  one domain by dropping Azure; we did not regain it by going to
  three nodes — three nodes is an availability win, not a
  confidentiality win).
- Confirm the recovery paths are concrete enough that Phase 6 can
  write the runbook directly from them.
- `ansible-lint` and `yamllint` pass on the inventory edit (one
  forward-declaration change; no functional impact).

## Caveats

- The two-domain failure model is materially weaker than the three-
  domain Azure design. A single Roboform compromise plus a single
  srvvault disk leak yields full secrets. Three nodes don't change
  this — the seal key is the same on all three; one disk is enough.
  Operator accepted this trade in exchange for removing the cloud
  dependency.
- **VRRP requires multicast on vmbr0**. Linux bridges pass multicast
  by default; UDM Pro doesn't filter intra-VLAN multicast by default;
  should just work. Verify with `tcpdump -i eth0 vrrp` on one
  srvvaultN during bring-up before declaring victory.
- **VIP must live on the vmbr0 subnet, outside the dnsmasq DHCP
  pool, with no MAC reservation** (it's a virtual IP, not a VM IP).
  Phase 6 picks the address; document it in `static-hosts.yaml`
  alongside the other operator-curated entries.
- **Network-partition VIP duplication**: if a partition isolates one
  srvvaultN from the other two, the minority node will hear no VRRP
  advertisements and may briefly claim MASTER on its own, holding a
  duplicate VIP on its segment. OpenBao Raft prevents writes on the
  minority side (no quorum), so client correctness is preserved
  even with a duplicate VIP — clients on the wrong side just get
  errors. Documented as a known property, not engineered around.
- **The static seal key reaches the repo via ansible-vault at
  bootstrap time.** The Phase 6 runbook needs an explicit step for
  "generate seal key, encrypt with ansible-vault, commit, copy
  passphrase to Roboform, verify decryption" — easy to forget, and
  the only window where the cleartext key exists outside Roboform's
  passphrase before the role takes over.
- **The "Operator admin path" boundary** (wrkdevwin → Jenkins agent
  VM → fleet) effectively removes `wrkdev` from the privileged path.
  `wrkdev` stays in inventory as a baseline-managed personal dev
  box; it just no longer carries automation keys or admin tokens.
  Capture this consequence in Phase 10's doc when it lands; for now
  it's only implicit in the new Phase 6 shape.
- **No OpenBao leader-change hook** is exposed by OSS OpenBao. The
  VIP-tracking mechanism polls `/v1/sys/leader` from Keepalived's
  `vrrp_script` every 2 s. If a future OpenBao release exposes a
  hook, simplify Keepalived's check to a hook-driven file flag.
- **The script-based VIP alternative** (each node runs a
  systemd-timer-driven script that adds/removes the VIP based on
  `is_self`) was considered and rejected for v1. Failover would be
  10s+ rather than 4–6s, and Keepalived already handles gratuitous
  ARP and edge cases that a custom script would have to reproduce.
  Kept as a fallback if VRRP turns out to be incompatible with the
  network for any reason.

## Commits

1. `docs/decisions.md` rewrites + `docs/plan.md` Phase 6 summary
   update + `ansible/inventories/prd/hosts.yml` forward-declaration
   change. Single commit; the inventory change is meaningless without
   the decisions, and the decisions reference the new names.
