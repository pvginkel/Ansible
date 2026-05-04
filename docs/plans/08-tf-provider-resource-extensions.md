# 08 — Add Ceph + ZFS resource types to the homelab provider

## Goal

Extend the `pvginkel/homelab` Terraform provider with three new resource
types so per-release `infrastructure.tf` in `/work/HelmCharts` can
declare durable storage objects directly:

- `homelab_ceph_rbd` — RBD image in a microceph pool.
- `homelab_ceph_cephfs_subvolume` — CephFS subvolume plus the path the
  static-PV module needs.
- `homelab_zfs_dataset` — dataset on a managed ZFS pool, addressed by
  pool + name.

Once these land, the static-PV TF modules from plan 09 can compose
{volume creation, K8s PV with `claimRef`, `prevent_destroy`} in a
single release apply, and the per-release deploy stops needing
`scripts/make-rbd.sh` / `scripts/make-cephfs.sh`.

This plan is independent of phase 6 — provider development can run on
operator-driven credentials kept in environment variables, then cut
over to OpenBao-issued credentials when phase 6 lands without
provider-side changes. No production migration happens until both these
resources and the deploy harness (plan 09) are ready.

## Decisions taken with the operator

- Provider-extension work is the prerequisite the operator can start on
  before phase 6.
- Same provider repo as the existing `homelab_dns_reservation`:
  `pvginkel/HomelabTerraformProvider`. Same version model and image
  embed as plan 04.
- ZFS pools are created up front by Ansible per-host. The provider only
  manages datasets within an already-existing pool. Pool → host mapping
  is provider-level config so resources don't repeat the host name.
- `prevent_destroy` is a call-site concern (set by the static-PV
  modules in plan 09), not a provider-side flag.

## Resource shapes

### `homelab_ceph_rbd`

Inputs:

| Name             | Type             | Required | Notes                                          |
|------------------|------------------|----------|------------------------------------------------|
| `pool`           | string           | yes      | Ceph pool name (e.g. `replicapool`).           |
| `name`           | string           | yes      | Image name (e.g. `k8s-prd-keycloak-db`).       |
| `size`           | string           | yes      | Bytes, or suffixed (`20G`, `500Mi`).           |
| `image_features` | list(string)     | no       | RBD features to enable; provider has a sane default. |
| `data_pool`      | string           | no       | For EC pools — primary stays in `pool`.        |

Computed:

- `id` — `<pool>/<name>`.

Operations:

- Create — `rbd create <pool>/<name> --size <bytes> --image-feature …`.
- Read — `rbd info <pool>/<name>`. Missing image → drift to recreate.
- Update — `rbd resize` for size grows. Shrink forces replace; caller
  opts in via `lifecycle { create_before_destroy }` or accepts the
  destroy-then-create.
- Delete — `rbd rm <pool>/<name>`.

### `homelab_ceph_cephfs_subvolume`

Inputs:

| Name              | Type   | Required | Notes                                              |
|-------------------|--------|----------|----------------------------------------------------|
| `volume`          | string | yes      | CephFS volume name (e.g. `cephfs`).                |
| `subvolume_group` | string | no       | Defaults to `_nogroup` (the implicit group).       |
| `name`            | string | yes      | Subvolume name (e.g. `k8s-prd-paperless-docs`).    |
| `size`            | string | yes      | Quota.                                             |
| `mode`            | string | no       | POSIX mode for the root.                           |
| `uid` / `gid`     | number | no       | Ownership for the root.                            |

Computed:

- `path` — output of `ceph fs subvolume getpath`. The static-PV module
  needs this for `csi.volumeAttributes.subvolumePath` and for
  `rootPath` when consumers mount the subvolume directly.
- `id` — `<volume>/<group>/<name>`.

Operations:

- Create — `ceph fs subvolume create <volume> <name> --size <bytes>
  [--group_name <group>] [--mode <mode>] [--uid …] [--gid …]`.
- Read — `ceph fs subvolume info` plus `getpath`.
- Update — `ceph fs subvolume resize` for size grows; metadata fields
  get the matching `setattr` calls.
- Delete — `ceph fs subvolume rm`.

### `homelab_zfs_dataset`

Inputs:

| Name          | Type           | Required | Notes                                                     |
|---------------|----------------|----------|-----------------------------------------------------------|
| `pool`        | string         | yes      | ZFS pool name (e.g. `zpool2`). Must exist.                |
| `name`        | string         | yes      | Dataset path within pool (e.g. `k8s/prd-paperless-data`). |
| `quota`       | string         | no       | `zfs set quota=…`.                                        |
| `recordsize`  | string         | no       | Default `128K`.                                           |
| `compression` | string         | no       | Default `lz4`.                                            |
| `mountpoint`  | string         | no       | Default `/<pool>/<name>`.                                 |
| `properties`  | map(string)    | no       | Forward-compat for arbitrary `zfs set` properties.        |

Computed:

- `id` — `<pool>/<name>`.
- `mountpoint_resolved` — what `zfs get mountpoint` reports after
  create. Useful for `static-zfs-pv` to set the `local:` path.

Operations:

- Create — `zfs create -o quota=… -o recordsize=… -o compression=… …
  <pool>/<name>`.
- Read — `zfs get -H all <pool>/<name>`. Round-trip every input.
- Update — `zfs set <prop>=<val>` per drifted property. No destroy on
  property change.
- Delete — `zfs destroy <pool>/<name>`.

## Provider configuration

Single `provider "homelab"` block accepts both Ceph and ZFS connection
config alongside the existing DNS reservation fields:

```hcl
provider "homelab" {
  # existing
  dns_reservation_url   = var.dns_reservation_url
  dns_reservation_token = var.dns_reservation_token

  # new — Ceph
  ceph_mon_endpoints = var.ceph_mon_endpoints  # list(string), sensitive
  ceph_user          = var.ceph_user           # default: "admin"
  ceph_keyring       = var.ceph_keyring        # string, sensitive

  # new — ZFS
  zfs_pools = {
    zpool2 = "srvk8s2"
    # zpool name -> hostname in .home for SSH
  }
  zfs_ssh_user = var.zfs_ssh_user              # default: "root"
  zfs_ssh_key  = var.zfs_ssh_key               # private key, sensitive
}
```

All sensitive inputs are marked `Sensitive: true` so they stay out of
plan output.

## Implementation notes

- **One provider binary, three new resources.** All land in
  `pvginkel/HomelabTerraformProvider` alongside `homelab_dns_reservation`.
  Same image-as-source-of-truth model as plan 04.
- **Acceptance tests against real infrastructure.** No mock providers —
  they hide too much. Use the prod microceph cluster with a dedicated
  test pool / volume; use a scratch VM with a small ZFS pool for the
  ZFS tests. Acceptance tests must not exercise destroy on data-bearing
  objects.
- **Read implementations must round-trip every input.** Without that,
  `terraform plan` after an out-of-band change shows nothing and the
  whole drift-detection story collapses.
- **Idempotent create.** A partial apply (network blip mid-create) must
  leave the resource recoverable on the next apply.
- **Credentials in tfstate.** Same posture as the existing setup —
  state remains a sensitive artefact protected by the state Git repo's
  access control.

## Verification

Per resource type, end-to-end against real infrastructure:

1. `terraform apply` creates the underlying object. `rbd info` /
   `ceph fs subvolume info` / `zfs list` show it with expected
   properties.
2. `terraform plan` after apply is a no-op.
3. Mutate the object out of band; `terraform plan` reports drift;
   `terraform apply` reverts.
4. Mutate an input that supports update (size grow, ZFS `compression`
   change); `terraform apply` is non-destructive and converges.
5. `terraform destroy` (without `prevent_destroy` at call site) removes
   the object cleanly.
6. With `prevent_destroy = true` set in the consumer module,
   `terraform destroy` halts before the provider is called.

## Caveats

- **Destroy on data is silent and final.** All three resource types
  share this property. The guardrail is `prevent_destroy = true` at
  the call site, which the static-PV modules from plan 09 set by
  default. This plan does not add the guard — only the call sites do.
- **CephFS path quirks.** The `path` computed attribute differs between
  `_nogroup` and explicit groups. Acceptance tests must cover both.
- **ZFS over SSH is order-sensitive.** Provider SSH session reuse
  matters with many datasets in one apply. Profile only if it bites at
  homelab scale.
- **No ZFS pool creation.** Pools are an Ansible concern (per-host,
  one-off, lives with the VM). The provider refuses to create or
  destroy pools. A missing pool is a configuration error, not a
  resource to create.

## Commits

1. This plan, here in `docs/plans/08-tf-provider-resource-extensions.md`.
2. (Operator-owned, separate repo) `pvginkel/HomelabTerraformProvider`:
   one commit per resource type, with acceptance tests. Provider
   README updated.
3. After all three land in the provider and the embedded image has
   propagated per plan 04, a smoke-check exercise from this repo's
   `terraform/scratch/` (one of each resource type), gated behind
   `terraform plan` not apply.
