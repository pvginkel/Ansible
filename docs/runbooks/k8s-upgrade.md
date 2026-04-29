# Rolling a microk8s cluster upgrade

How to roll an OS + microk8s upgrade across a microk8s cluster while keeping workloads scheduled. Driven by [`playbooks/update-k8s.yml`](../../ansible/playbooks/update-k8s.yml).

## What it does

A pre-flight assertion runs first per host: refuses to step microk8s by more than one minor (e.g. `1.30` → `1.32`) or downward. Multi-minor upgrades are multi-roll: bump `microk8s_channel` one minor in inventory, run the playbook, soak, advance.

Then for each cluster member, one node at a time (`serial: 1`):

1. **Drain** the node from the cluster primary, ignoring DaemonSets and accepting emptyDir loss. Skipped on single-node clusters.
2. **Snap-refresh microk8s** to the channel pinned in `group_vars/k8s_<cluster>.yml` (or overridden in `host_vars/<host>.yml`). Forces a refresh every run via `state: refreshed` — picks up patch revisions within the same channel (e.g. `1.32.13` → `1.32.14`); no-op when already at the latest revision in the channel.
3. **`apt full-upgrade`** — picks up kernel, security, and package updates.
4. **Reboot** if `/var/run/reboot-required` is present (kernel/glibc/etc.).
5. **Wait for microk8s `Ready`** — both after snap-refresh and after reboot.
6. **Uncordon** the node from the primary. Skipped on single-node clusters.

If any step fails on a node, the playbook stops; nothing else moves. The cordoned/drained node stays cordoned until the operator either uncordons by hand or re-runs after fixing the cause.

## When to run

- Routinely, to pick up Ubuntu security patches and microk8s patch versions.
- After a microk8s LTS channel bump in inventory (e.g. `1.32/stable` → `1.34/stable`).

## Prerequisites

- Both SSH identities loaded per [`operator-workstation.md`](operator-workstation.md).
- Cluster members reachable; primary node responsive (`microk8s status` is `running: True`).

## Run

From `ansible/`:

### Smoke against scratch (always do this first if the playbook has changed)

```sh
poetry run ansible-playbook playbooks/update-k8s.yml \
    -i inventories/scratch --limit k8s_scratch
```

Two-node cluster, exercises drain/uncordon. Snap-refresh is a no-op (scratch already runs the pinned channel); apt may have updates and a reboot to apply.

### `wrkdevk8s` (single-node smoke)

```sh
poetry run ansible-playbook playbooks/update-k8s.yml \
    -i inventories/prd --limit wrkdevk8s
```

Single node — drain and uncordon skip. Snap-refresh is a no-op while `wrkdevk8s` is pinned to `1.30/stable` in `host_vars/`; apt full-upgrade still runs.

### `k8s_prd` (real exercise)

```sh
poetry run ansible-playbook playbooks/update-k8s.yml \
    -i inventories/prd --limit k8s_prd
```

Three-node cluster, real drain/uncordon cycle. Roll takes a few minutes per node × 3 nodes (assuming reboots).

## Refreshing addons after a microk8s upgrade

Addons (DNS, MetalLB, dashboard, etc.) and the CNI ship with manifests pinned to whatever microk8s revision was *current at install time*; they don't auto-update when the snap refreshes. After a microk8s minor bump you'll want to refresh them, but **don't** bundle this with the snap upgrade — soak the cluster on the new microk8s version for a few days first so any regression in the addons is isolated from regressions in microk8s itself.

```sh
poetry run ansible-playbook playbooks/refresh-k8s-addons.yml \
    -i inventories/prd --limit k8s_prd
```

Per cluster, on the primary only:

1. `microk8s addons repo update core` — pulls fresh manifests.
2. Iterates `microk8s_addons` from group_vars: `microk8s disable <addon>` then `microk8s enable <addon>`.
3. Waits for microk8s `Ready`.
4. Re-applies the role's `metallb.yml` task — the re-enable resets `default-addresspool` to a sentinel range; this restores it to your real `microk8s_metallb_pool_addresses`.

Brief unavailability per addon during the disable/enable cycle. Run during a maintenance window.

## Drain blocked by a PodDisruptionBudget

Drain uses `kubectl drain --ignore-daemonsets --delete-emptydir-data --timeout=300s`, which honours PodDisruptionBudgets. A PDB that can't be satisfied (e.g. a single-replica Deployment with `minAvailable: 1`, where evicting the only pod would violate the budget) blocks drain indefinitely; after the 5-minute timeout the playbook fails and the node stays cordoned.

Symptom in the run output:

```
error when evicting pods/"<name>" -n "<namespace>": Cannot evict pod as it would violate the pod's disruption budget.
```

Recovery for the in-flight run:

```sh
microk8s kubectl uncordon <stuck-node>
microk8s kubectl delete pod <stuck-pod> -n <stuck-namespace> --grace-period=0 --force
poetry run ansible-playbook playbooks/update-k8s.yml \
    -i inventories/prd --limit '<remaining-nodes>'
```

The force-delete bypasses the PDB by skipping the eviction API entirely. The Deployment recreates the pod on a still-schedulable node.

Long-term fix: audit `HelmCharts` for charts whose PDB blocks drain. For single-replica services, drop the PDB or switch from `minAvailable: 1` to `maxUnavailable: 1` (allows the one pod to be unavailable during a drain — same effect as no PDB during scheduled maintenance, but still protects against accidental concurrent disruption).

## Workstation DNS during a roll

If your operator workstation's DNS points only at a resolver hosted on the cluster being rolled, every node-reboot window will black out resolution from the workstation — including the workstation's connection to *other* nodes the playbook is trying to mutate. Make sure the workstation has a secondary resolver pointing somewhere not hosted on the cluster (LAN router, public DNS) before running. DHCP option 6 with both resolvers is the obvious answer.

## Rollback

A microk8s refresh that goes sideways:

```sh
ssh <bad-node>
sudo snap revert microk8s
sudo microk8s status --wait-ready --timeout 120
microk8s kubectl uncordon <bad-node>
```

`snap revert` rolls microk8s to the previous installed revision. To roll further back, inspect `snap info microk8s` for available revisions and `snap refresh microk8s --revision=<rev>`.

For an apt-induced regression, fix the offending package or kernel manually (`apt install <previous-version>`, `apt-mark hold <package>`); the playbook does not enforce package versions.

## Don't use this to channel-bump `wrkdevk8s` to `1.32/stable`

`wrkdevk8s` runs `1.30/stable` until the Phase 4a step 11 rebuild brings it clean to `1.32/stable` with the deterministic-MAC rotation, new CIDRs, and clean addon set. The rebuild handles the channel transition; this playbook does not. Until then, `microk8s_channel` is pinned to `1.30/stable` in `host_vars/wrkdevk8s.yml` so snap-refresh is a no-op even if the playbook is run against it.

## Verify

After a roll:

```sh
poetry run ansible -i inventories/prd k8s_prd -m shell \
    -a 'microk8s kubectl get nodes -o wide; uname -r' \
    --become-user=root
```

All nodes should report `Ready` and the same kernel version.
