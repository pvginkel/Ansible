# `microceph` role

Installs and configures [microceph](https://documentation.ubuntu.com/canonical-microceph/) on an Ubuntu host: the snap pinned to a Ceph-release channel, a single-node cluster bootstrap, a raw-disk OSD, single-node pool sizing, daemon memory caps, and the RGW / CephFS / RBD capabilities.

Today it drives the **single-node dev cluster** co-located on `srvk8sdev` (`ceph_dev`), standing up isolated dev storage so HelmCharts iteration stops churning RBD images / RGW users on the prod Ceph cluster. The 3-node prd fleet (`srvceph1/2/3`) comes under it in **Phase 5** with `serial: 1` + drain hooks — the multi-node join path is intentionally not built yet.

## Mental model

microceph (Ansible bring-up tier, per `decisions.md` "Tool split") owns the Ceph *cluster*: the snap, the cluster membership, the OSDs, pool sizing, and which daemons run. Per-application durable resources on top — RBD images, CephFS subvolumes, RGW users — are Terraform's job via the homelab provider, alongside the Helm chart. This role stops at "a healthy cluster with the right capabilities present."

## What it does

1. **Install** — `snap wait system seed.loaded`, then the microceph snap (strict confinement) at `microceph_channel`.
2. **Bootstrap** — `microceph cluster bootstrap` once (guarded on `microceph status`), then waits for the cluster to serve.
3. **OSD** — resolves `microceph_osd_scsi_index` to a guest device by HCTL (same lookup as `managed_filesystems`), and `microceph disk add`s it if still blank.
4. **Config** — single-node size-1 pool defaults + reconciles existing pools to size 1; applies `osd_memory_target` / `osd_memory_target_min` / `mds_cache_memory_limit`, restarting the affected daemon on change (the snap's glibc malloc won't return freed RSS otherwise).
5. **Services** — enables RGW, creates + initialises RBD pools, and (optionally) enables an MDS and creates a CephFS filesystem.

## Inputs

See `defaults/main.yml`. Per environment in `group_vars/ceph_<env>.yml`; today only `ceph_dev`.

| Variable | Purpose |
|---|---|
| `microceph_channel` | Ceph-release channel — `squid/stable` = Ceph 19. Empty fails loud. |
| `microceph_single_node` | size-1 pools so one OSD reaches HEALTH_OK. |
| `microceph_osd_scsi_index` | PVE scsi slot of the **raw** OSD disk (absent from `managed_filesystems_volumes`). |
| `microceph_osd_wipe` | zap the disk before add (destructive; default off). |
| `microceph_osd_memory_target`, `microceph_mds_cache_memory_limit` | daemon memory caps; restart-on-change. |
| `microceph_enable_rgw`, `microceph_rgw_port` | RADOS Gateway (S3). |
| `microceph_enable_cephfs`, `microceph_cephfs_*` | CephFS filesystem + its pools. |
| `microceph_rbd_pools` | block pools to create + `rbd pool init`. |

## Constraints

- **The OSD disk must be raw** — never list it in `managed_filesystems_volumes`. The role refuses to touch a disk that already carries partitions/children (re-add needs `microceph_osd_wipe`).
- **Single-node only today.** No cluster-join logic; Phase 5 adds it.
- **Channels are Ceph release codenames, not Ubuntu releases** — `reef`=18, `squid`=19, `tentacle`=20. Pin a named channel, never the floating `latest`.

## Running it

`srvk8sdev`'s baseline + microk8s converge through `site-k8s.yml`; this role's storage layer converges through `playbooks/site-ceph.yml`:

```
ansible-playbook playbooks/site-ceph.yml --limit srvk8sdev
```

On a fresh rebuild, run the k8s convergence first (baseline lands there), then `site-ceph.yml`.
