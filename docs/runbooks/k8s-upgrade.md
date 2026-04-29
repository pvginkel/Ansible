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
