# Homelab handover — hardware and platform

Physical and platform-level description of the homelab: what the hardware is, how it is
wired, what runs where, and the failure characteristics a new operator needs to know before
touching anything.

**Provenance.** The hardware facts here were read from the live machines on **2026-08-09**.
They are deliberately *not* derived from the repo, because the repo does not record them:
there are no CPU, RAM, or physical-disk facts for `pve`/`pve1`/`pve2` anywhere in `Ansible`
or `AnsibleSpecs`, and `host_vars/pve1.yml` and `host_vars/pve2.yml` do not exist. This
document is the only written record of the physical layer. Re-verify it after any hardware
change — nothing regenerates it.

The logical layer (networks, VM shapes, MAC scheme, affinity classes) *is* repo-derived and
is cited to its source, so those sections stay true as long as the code does.

---

## 1. Physical nodes

One Proxmox cluster named `home`: three physical nodes, corosync/knet transport, 3/3
quorate. PVE 8.4.1, kernel 6.8.12-11-pve on all three.

| | **pve** | **pve1** | **pve2** |
|---|---|---|---|
| Chassis | MSI MS-7D43 (custom build) | Dell OptiPlex 7050 | Dell OptiPlex 7050 |
| CPU | Intel i5-14500 — 14C/20T (6 P-core + 8 E-core) | Intel i5-7500 — 4C/4T @ 3.4 GHz | Intel i5-7500 — 4C/4T |
| RAM | 96 GB (94 GiB visible) | 32 GB (31 GiB visible) | 32 GB (31 GiB visible) |
| LAN address | 10.1.0.20 | 10.1.0.21 | 10.1.0.22 |
| Backplane address | 192.168.188.20 | 192.168.188.21 | 192.168.188.22 |

Cluster totals: ~22 physical cores / 28 threads, ~160 GB RAM.

**The cluster is deliberately asymmetric.** `pve` carries the great majority of the
workload; `pve1` and `pve2` are small office SFFs whose main job is to provide a third
physical failure domain so Ceph, the microk8s dqlite quorum, and the OpenBao Raft quorum
each have three independent hosts. Do not reason about the cluster as if the nodes are
interchangeable — they are not, and capacity planning that assumes they are will be wrong
(see §6).

## 2. Storage

Every node follows the same pattern: **one NVMe carries the hypervisor and all virtual
disks; one 2 TB SATA SSD is passed straight through to that node's Ceph OSD VM.**

| Node | Hypervisor NVMe | Ceph OSD disk (passthrough) | Additional |
|---|---|---|---|
| pve | Samsung 980 PRO 1 TB → `local` (98 GiB dir) + `local-lvm` (790 GiB LVM-thin) | Samsung 870 EVO 2 TB, serial `…128911L` → **srvceph3** | Samsung 980 500 GB → passthrough to **srvk8s1** (backs `zpool2`); Seagate ST2000LM015 2 TB **HDD** → `local-backup`; Samsung 870 EVO 2 TB at `/dev/sda` = intentional uncabled spare |
| pve1 | Samsung 980 500 GB → `local` (98 GiB) + `local-lvm` (334 GiB thin) | Samsung 870 EVO 2 TB, serial `…128906Y` → **srvceph1** | `local-backup` declared but disabled |
| pve2 | Samsung 980 500 GB → same split | Samsung 870 EVO 2 TB, serial `…128908E` → **srvceph2** | `local-backup` declared but disabled |

`/dev/sda` on `pve` is wiped and bound to nothing on purpose — it is cabled so a future use
needs no physical work. Inspection tooling will flag it as unused; that is expected and
recorded in `ansible/inventories/prd/host_vars/pve.yml`.

**Ceph** is therefore 3 × 2 TB SSD OSDs, one per physical host, at 3× replication — one full
copy of the data per physical machine. The passthrough disks use `cache=writeback` (host RAM
absorbs BlueStore commits; the replication factor is what makes that acceptable) and are
always excluded from vzdump.

**Datastores:** `local` (ISO/cloud-image/snippets), `local-lvm` (all managed VM disks and EFI
disks), `local-backup` (vzdump target, exists on `pve` only).

## 3. Network

Three logical fabrics across two physical NICs per node.

| Fabric | Subnet | Speed | Purpose |
|---|---|---|---|
| `vmbr0` untagged | 10.1.0.0/16, gw 10.1.0.1 | 1 GbE | House LAN. Default route, DNS, DHCP. Internet-facing. |
| `vmbr0` VLAN tag 2 | 10.2.0.0/16 | same physical wire | Kubernetes workload network. MetalLB L2 pool `10.2.1.1-10.2.1.199`. |
| `vmbr1` | 192.168.188.0/24 | **2.5 GbE** | Ceph and inter-node backplane. Not routable from the house LAN. |

Physical NIC assignment differs by node:

- **pve** — `enp4s0` (1 GbE) → `vmbr0`; `enp3s0` (2.5 GbE, onboard) → `vmbr1`.
- **pve1 / pve2** — `enp0s31f6` (onboard Intel, 1 GbE) → `vmbr0`; `enx107c61…` (**USB 2.5 GbE
  adapter**) → `vmbr1`. Both also have Wi-Fi (`wlp2s0`), administratively down.

The USB backplane NICs on the two Dells are a load-bearing detail: the Ceph replication path
on those nodes runs over a USB adapter. Treat it as a suspect whenever storage latency or
flapping is being investigated.

Bridge configuration is **out of band** — no Ansible role manages `/etc/network/interfaces`,
so a rebuilt PVE node needs its bridges configured by hand.

### Addressing and naming

- DNS search domain is `.home`; DHCP and DNS are served by **dnsmasq running as an in-cluster
  Kubernetes pod** (2-replica StatefulSet). Consequence: bootstrap-critical hosts (k8s nodes,
  Ceph nodes, OpenBao nodes) must not resolve through it, and carry static netplan plus
  external resolvers instead. This is the single most important circular-dependency rule in
  the estate — see `AnsibleSpecs/decisions.md`, "DNS and hostnames".
- **MAC scheme**: rebuilt VMs get deterministic locally-administered MACs, `02:A7:F3:<vmid
  hi>:<vmid lo>:<nic index>`. Legacy pre-rebuild VMs (the three Ceph nodes) keep their
  Proxmox-generated `BC:24:11:…` MACs verbatim.
- **Keepalived VIPs** on the LAN (`ansible/inventories/prd/group_vars/all/vips.yml`):

  | Name | Hostname | Address | VRRP router ID |
  |---|---|---|---|
  | kubernetes_api | `kubernetes-api.home` | 10.1.0.37 | 51 |
  | ceph | `ceph.home` | 10.1.0.38 | 52 |
  | openbao | `secrets.home` | 10.1.0.39 | 53 |

## 4. What runs on the hardware

Terraform manages 12 production VMs (`terraform/prd`) plus 2 disposable scratch nodes
(`terraform/scratch`, currently not provisioned). Several legacy guests exist in Proxmox only.

### Terraform-managed production VMs

| VM | vmid | Node | vCPU | RAM | Disks | Role |
|---|---|---|---|---|---|---|
| srvk8s1 | 910 | pve | 8 | 16 GiB | 20G + 80G, NVMe passthrough (`zpool2`) | microk8s node |
| srvk8s2 | 911 | pve1 | 3 | 16 GiB | 20G + 80G + 40G (`zpool3`) | microk8s node |
| srvk8s3 | 912 | pve2 | 3 | 16 GiB | 20G + 80G + 40G (`zpool4`) | microk8s node |
| srvk8s4 | 916 | pve | 8 | 20 GiB | 20G + 80G + 100G (`zpool5`) | microk8s node, the only `interactive` VM |
| srvk8sdev | 919 | pve | 4 | 12 GiB | 60G + 20G + 20G | single-node dev k8s + dev Ceph (**currently stopped**) |
| srvceph1 | 113 | pve1 | 3 | 10 GiB | 32G + 100G, 2 TB SSD passthrough | Ceph OSD/mon |
| srvceph2 | 114 | pve2 | 3 | 10 GiB | 32G + 100G, 2 TB SSD passthrough | Ceph OSD/mon |
| srvceph3 | 115 | pve | 3 | 10 GiB | 32G + 100G, 2 TB SSD passthrough | Ceph OSD/mon |
| srvvault1 | 913 | pve | 2 | 1 GiB | 24G | OpenBao Raft peer |
| srvvault2 | 914 | pve1 | 2 | 1 GiB | 24G | OpenBao Raft peer |
| srvvault3 | 915 | pve2 | 2 | 1 GiB | 24G | OpenBao Raft peer |
| srviac | 920 | pve | 2 | 3 GiB | 32G | Jenkins agent / Terraform+Ansible runner |

Kubernetes is microk8s **v1.35.6** on Ubuntu 24.04.4, containerd 2.1.6, Calico in VXLAN mode.
The three Ceph VMs are configured as `1 core × 3 sockets` rather than `3 × 1` — three vCPUs
either way; the shape is an artifact of adoption, not a deliberate topology choice.

The Ceph production fleet is **not yet Ansible-managed** — `playbooks/site-ceph.yml` targets
`ceph_dev` only. The prd OSD disk identities live solely in `terraform/prd/vms.tf`.

### Proxmox-only guests (not in Terraform, not Ansible-managed)

On `pve`: `srvhomeassistant` (running, 6 GiB), `wrkdev` (operator workstation VM, running,
6 GiB — Ansible-managed baseline but Terraform-unmanaged), `wrkdevwin` (Windows, running,
18 GiB), plus stopped `srvhassiodev`, `srvtest`, `wrktql`, `wrktql10`, `wrkmariska`.

### CPU affinity

Two classes, applied **on `pve` only** because it is the only node with a core layout worth
zoning. Defined in `terraform/prd/vms.tf`, declared per VM as `workload_class` in host_vars.

| Class | Cores | Maps to |
|---|---|---|
| `interactive` | `0-11` | the i5-14500's 6 P-cores with hyperthreading |
| `background` | `12-19` | the 8 E-cores |

`srvk8s4` is the only `interactive` VM. VMs on `pve1`/`pve2` are unpinned — those CPUs have
no performance/efficiency split to exploit.

## 5. Access

- **SSH to managed hosts** uses `~/.ssh/id_ed25519_ansible` (user `ansible`) and
  `~/.ssh/id_ed25519_pve` (user `root` on the PVE nodes), provisioned by
  `scripts/kubecoder-keys.sh` via `kc project setup`. Hosts present an **SSH CA certificate**,
  not a plain host key; the CA is `ansible/files/known_hosts.d/homelab`, which is why SSH
  commands are run from the `ansible/` directory with the option pile mirroring
  `ansible.cfg`'s `ssh_args`.
- **Kubernetes**: node-level operations (`cordon`, `drain`, anything writing a Node object)
  cannot be done with the mounted kubeconfigs — they have no rights on the cluster-scoped
  `nodes` resource. Use `sudo microk8s kubectl` over SSH on a node instead.
- **OpenBao** is at `secrets.home:8200`. Reachable, but no token is provisioned in the dev
  environment by default.
- **Terraform** state reads work from the dev pod; `plan`/`apply` do not, because the Proxmox
  credentials are not in the secret catalog. Applies run through the `IaC/*` Jenkins
  pipelines or by hand on `srviac`.

## 6. Failure characteristics — read this before planning capacity

Four facts that together define what this cluster can and cannot survive.

**No Proxmox HA, no replication.** `ha-manager` has no resources configured and `pvesr` has
no jobs. Nothing migrates or restarts automatically. A node loss means its guests are down
until a human intervenes.

**Every node is 85–87% RAM-committed, with ballooning disabled fleet-wide**
(`memory_floating_mb` is unset on every VM, so allocations are fixed):

| Node | Allocated to running VMs | Physical | Committed |
|---|---|---|---|
| pve | 80 GiB | 94 GiB | ~85% |
| pve1 | 27 GiB | 31 GiB | ~87% |
| pve2 | 27 GiB | 31 GiB | ~87% |

The consequence is the important part: **there is nowhere to fail a node over to.** If `pve1`
dies, its 27 GiB of guests do not fit on `pve` (14 GiB headroom) or on `pve2` (4 GiB). The
design absorbs a node loss through *service-level* redundancy — Ceph 3× replication, the k8s
dqlite quorum, the OpenBao Raft quorum — not through VM mobility. Recovery from a dead node
is "repair or rebuild that node", never "move its VMs".

**VMs on `pve1` and `pve2` are not backed up at all.** The single vzdump job
(`backup-774ec731-f7bd`, daily 04:00, snapshot mode, zstd, keep-last=3) is pinned to node
`pve` and targets `local-backup`, which only exists there. So `srvceph1`, `srvceph2`,
`srvk8s2`, `srvk8s3`, `srvvault2`, `srvvault3` have no vzdump artifact. This is consistent
with the code — the `managed-vm` module sets `backup=true` only when the VM's node declares
`pve_node_backup_datastore` — and defensible, since all six are quorum members rebuildable
from their peers. It is not obvious from the outside, so it belongs in any handover.

`srvvault1` is on `pve` but is **force-excluded** from backup (`exclude_from_backup=true`),
deliberately, so the OpenBao seal key never lands in the same vzdump artifact as the Raft
data. Do not "fix" this.

**Backups land on the one spinning disk in the estate.** `local-backup` is the Seagate 2 TB
HDD on `pve`. It is a single disk with no redundancy, and it is the reason `proxmox_host`
tunes `vm.dirty_bytes`/`vm.dirty_background_bytes` down — dumping to a slow target was
pushing the host into swap.

### Current headroom warnings

- `pve` `local-lvm` is at **89.7%** (743 of 829 GB). LVM-thin pools behave badly at 100%;
  this is the number to watch.
- `pve` `local-backup` is at **77.8%** (1.50 of 1.92 TB).
- `pve1`/`pve2` `local-lvm` are at ~55% — comfortable.

## 7. Known documentation drift

- **`AnsibleSpecs/decisions.md` describes `vmbr1` as a "10 Gb backplane". It is 2.5 GbE**, on
  all three nodes, and on the two Dells it runs over a USB adapter. The same document carries
  a standing "audit that vmbr1 actually carries the traffic it's meant to" item, so the
  figure is most likely aspirational rather than regressed — but anything sized against
  10 Gb is sized wrong.
- The NIC-shape table in that section lists "everything else: vmbr0 only". In practice
  `srvk8sdev` and `srviac` also carry a `vmbr1` interface.
- `ansible/inventories/prd/hosts.yml` says the `pve_vms` group is "read by the `proxmox_host`
  role … for affinity reconciliation". It is not — affinity is written by Terraform via the
  `bpg/proxmox` provider. The group's real consumer is `terraform/prd/vms.tf`.

## 8. Out of scope / no visibility

- **The UDM Pro (`router.home`, 10.1.0.1) and the managed switch are not managed by this
  estate** and there is no automated visibility into them. `AnsibleSpecs/decisions.md` lists
  "UDM Pro + managed switch" as explicitly *deferred*. The router responds on 22/80/443/8443
  and its SSH offers `publickey,keyboard-interactive`, but no credential for it exists in this
  environment. Nothing in either repo records its configuration, firmware, VLAN definitions,
  or port layout — including the VLAN 2 trunking that the Kubernetes workload network depends
  on. **A new operator must obtain UniFi access separately.**
- Home Assistant, Windows VMs, end-user devices and IoT are out of scope per
  `decisions.md` "Scope".
- Ceph cluster health and capacity utilisation are **not captured here** — the Ceph VMs were
  not reachable over SSH from the environment this document was written in. Get them from
  `microceph.ceph -s` / `ceph osd df` on a Ceph node.
- No rack, power, UPS, or physical-site information is recorded anywhere.
