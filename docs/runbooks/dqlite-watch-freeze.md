# Runbook: microk8s watch-cache freeze (k8s-dqlite watch stall)

## Symptom

One or more of:

- A Deployment won't roll out — `spec.replicas` / `generation` advances but
  `status.observedGeneration` lags and no pod is created. Scaling and
  `rollout restart` do nothing.
- `kubectl top` intermittently returns `ServiceUnavailable`; the
  `v1beta1.metrics.k8s.io` APIService flaps.
- Headlamp / other list-watch-heavy clients are slow or stale.
- Plain `kubectl get` looks **fine** (correct values) — which is why this
  hides.

## Cause

On microk8s **1.34–1.35**, the `k8s-dqlite` watch poll loop can die on a
transient (e.g. a brief network blip) and stay dead until the daemon is
restarted. A dead watch freezes that apiserver's **watch cache** at an old
`resourceVersion`. Controllers (deployment, replicaset, scheduler) and
aggregated APIs list/watch from the frozen cache and act on stale state,
while quorum reads (the default for a single-object `kubectl get`) read
through to the datastore and stay correct.

Upstream: [k8s-dqlite#364](https://github.com/canonical/k8s-dqlite/issues/364),
[microk8s#5386](https://github.com/canonical/microk8s/issues/5386). Fix:
[k8s-dqlite PR #365](https://github.com/canonical/k8s-dqlite/pull/365)
("keep poll loop alive across watch-query timeouts") — **unmerged** as of
this writing, so it is in **no released microk8s version** (not 1.35.5, not
1.36). Only **≤1.32** predates the regression.

## Confirm it (read-only)

Compare a cache-served read against a quorum read of the same hot object.
A freeze shows the cache pinned at an old `resourceVersion` while the
quorum read is current:

```sh
LEASE=/apis/coordination.k8s.io/v1/namespaces/kube-system/leases/kube-controller-manager
# cache-served (watch cache):
kubectl get --raw "$LEASE?resourceVersion=0" | python3 -c 'import sys,json;print(json.load(sys.stdin)["metadata"]["resourceVersion"])'
# quorum (datastore):
kubectl get --raw "$LEASE"                   | python3 -c 'import sys,json;print(json.load(sys.stdin)["metadata"]["resourceVersion"])'
```

Diverging (cache stuck, quorum advancing across repeated samples) = frozen.
To find which node, run the cache read against each node's **own** apiserver
(`ssh <node> 'microk8s kubectl get --raw "$LEASE?resourceVersion=0"'`); each
apiserver has its own cache.

## Recover

Restart the **datastore**, not kubelite. Restarting kubelite/the apiserver
does **not** durably help — the watch client reconnects to the same wedged
watch server and re-freezes within ~1–2 min. Restarting `k8s-dqlite` resets
the watch source.

**Automated (default):** a per-node systemd timer, `dqlite-watchdog.timer`,
installed on every apiserver node by the `microk8s` role
(`tasks/watchdog.yml`). Every ~5 min it probes the node's **own** apiserver
for the freeze signature and, on that signature alone, restarts the node's
`k8s-dqlite`. Each node heals itself — no orchestrator, no cross-node
coordination; `RandomizedDelaySec` jitters the nodes so they don't restart
in lockstep. It only ever acts on the unambiguous frozen signature, so a
healthy node is a no-op. Watch it work:

```sh
ssh <node> 'systemctl list-timers dqlite-watchdog.timer; journalctl -u dqlite-watchdog -n 20 --no-pager'
```

A `FROZEN … restarting` / `restarted …` line is a recovery; `rc=2`
(apiserver unreachable) leaves the unit failed for a human to look at —
that is a bigger fault than a watch freeze, not something to clear by
restarting dqlite blind.

**Manual**, if you need to force it now — restarting the datastore on the
frozen node is the whole fix:

```sh
ssh <node> 'sudo systemctl restart snap.microk8s.daemon-k8s-dqlite'
# wait ~30s; re-run the cache-vs-quorum check on <node> until lag→0
```

On an HA cluster, if more than one node is frozen, roll them one at a time
and wait for quorum (`microk8s status` shows 3 datastore masters) between
each so you never drop more than one voter. (The per-node timer trusts
`RandomizedDelaySec` to space restarts instead of gating on quorum.)

After recovery the stuck Deployment reconciles on its own; pods already
scheduled keep running regardless. Note a freeze can take dependent
workloads down with it (e.g. a pod that hard-exits when its backing service
is briefly unreachable) — those recover once their dependency is back.

## Note

This is a workaround for a bug with no released fix. Track PR #365; when it
ships in a 1.35.x build, upgrade to that and the watchdog becomes a
defense-in-depth safety net rather than a routine necessity.
