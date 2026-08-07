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
- After a microk8s channel bump in inventory (e.g. `1.35/stable` → `1.36/stable`).

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

### `srvk8sdev` (single-node smoke)

```sh
poetry run ansible-playbook playbooks/update-k8s.yml \
    -i inventories/prd --limit srvk8sdev
```

Single node — drain and uncordon skip. Snap-refresh exercises the channel pinned in `group_vars/k8s_dev.yml`; apt full-upgrade still runs.

### `k8s_prd` (real exercise)

```sh
poetry run ansible-playbook playbooks/update-k8s.yml \
    -i inventories/prd --limit k8s_prd
```

Four-node cluster, real drain/uncordon cycle. Roll takes a few minutes per node × 4 nodes (assuming reboots).

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

`dns` is the one to plan around: the disable step deletes the CoreDNS Deployment outright, so for that window nothing in the cluster resolves anything — a different order of severity from the dashboard being briefly gone. Anything that reconnects by hostname during the gap (and anything whose liveness probe depends on resolution) can take collateral restarts. The re-enable also resets the Deployment to the addon's stock spec, so any local scaling is lost the same way `default-addresspool` is at step 4 — the role's `coredns.yml` re-asserts the Corefile on the next `site-k8s.yml` converge, but nothing re-asserts the replica count.

## Re-evaluate the dqlite watch-freeze watchdog

Every time you bump the microk8s channel, re-check whether the per-node
`dqlite-watchdog.timer` is still needed. It works around the `k8s-dqlite`
watch-stall bug ([k8s-dqlite#364](https://github.com/canonical/k8s-dqlite/issues/364)
/ [microk8s#5386](https://github.com/canonical/microk8s/issues/5386)),
introduced in 1.34 and unfixed in any release as of this writing — fix
[PR #365](https://github.com/canonical/k8s-dqlite/pull/365) is unmerged.
Once the cluster runs a microk8s version that carries the fix, the watchdog
is dead weight: remove it (`tasks/watchdog.yml`, its templates, and the
`microk8s_watchfreeze_*` / `microk8s_watchdog_*` defaults) or keep it only
as a deliberate defense-in-depth net. See
[`dqlite-watch-freeze.md`](dqlite-watch-freeze.md).

## Re-evaluate the Calico CNI-token refresh job

Every time you bump the microk8s channel, re-check whether the weekly
`iac-scheduled-calico` job — a rolling-restart of the `calico-node`
DaemonSet, [`playbooks/refresh-calico-token.yml`](../../ansible/playbooks/refresh-calico-token.yml)
— is still needed. It works around
[projectcalico/calico#8777](https://github.com/projectcalico/calico/issues/8777):
on long-uptime `calico-node` pods (worker nodes especially) `token_watch.go`
silently stops rewriting the CNI kubeconfig token, so the bounded
ServiceAccount token expires and new pod sandboxes on that node fail with
`error getting ClusterInformation: connection is unauthorized: Unauthorized`
(existing pods keep networking; the node stays `Ready`). Restarting
`calico-node` writes a fresh token; the weekly roll caps every pod's uptime
below the expiry window. First hit `srvk8s4` (the cluster's only `--worker`
node) on 2026-07-04.

Once the cluster runs a Calico version that carries the fix, the job is
dead weight: retire it (delete `Jenkinsfile.iac-scheduled-calico` and
`playbooks/refresh-calico-token.yml`, unwire the job) or keep it only as a
deliberate defense-in-depth net. Check the bundled Calico version with
`microk8s kubectl -n kube-system get ds calico-node -o jsonpath='{.spec.template.spec.containers[0].image}'`
and cross-reference the issue.

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

## Drain that succeeds and takes a service down anyway

The inverse of the section above, and the more dangerous one: a single-replica Deployment with **no** PDB is evicted immediately and silently. Drain reports success, the roll continues, and the service is down until the pod reschedules elsewhere and passes its readiness probe. There is no error in the run output and nothing to grep for — the roll looks clean.

Rolling-update settings do not help here. `maxSurge` / `maxUnavailable` govern rolling *updates*; an eviction is a delete, so the replacement pod starts only after the old one is gone.

**In-cluster DNS is the live instance of this.** `kube-system/coredns` runs one replica with no PDB, no anti-affinity, and no `topologySpreadConstraints` — whatever the microk8s `dns` addon ships. While it is being rescheduled, nothing in the cluster resolves anything. Note this is *not* the same concern as the workstation-DNS section below: that one is about the operator's own resolution path through dnsmasq; this one is about pod-to-pod resolution inside the cluster.

Check where it is sitting before a roll:

```sh
microk8s kubectl -n kube-system get pod -l k8s-app=kube-dns -o wide
```

CoreDNS tolerates only `CriticalAddonsOnly`, and `srvk8s4` carries a `homelab.local/performance=high:NoSchedule` taint, so it can only land on `srvk8s1`/`2`/`3` — one of the three nodes the roll will drain. The gap is short (the image is already present and the readiness probe polls every 10s) but it is a real, unannounced outage of cluster DNS on every roll.

Mitigation for a single run: scale up before starting and back down after.

```sh
microk8s kubectl -n kube-system scale deployment/coredns --replicas=2
```

This is undeclared drift — `refresh-k8s-addons.yml` deletes and recreates the Deployment, which resets it to one replica. The durable fix is to declare the replica count, anti-affinity, and a PDB in the `microk8s` role; that is tracked as its own change and not yet done.

When auditing for others in this class, note that the "audit `HelmCharts`" pointer above is not enough on its own — CoreDNS is a kube-system addon, not a chart, and the same is true of anything else the addons install.

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

## Verify

After a roll:

```sh
poetry run ansible -i inventories/prd k8s_prd -m shell \
    -a 'microk8s kubectl get nodes -o wide; uname -r' \
    --become-user=root
```

All nodes should report `Ready` and the same kernel version.
