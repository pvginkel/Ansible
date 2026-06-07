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

**Automated (preferred):** the `iac-dqlite-watchdog` Jenkins job runs
`playbooks/recover-dqlite-watch.yml` from srviac every ~5 min — it probes
every node and rolling-restarts `k8s-dqlite` only on a frozen node, one at a
time. To run it on demand:

```sh
cd ~/source/Ansible/ansible && poetry run ansible-playbook playbooks/recover-dqlite-watch.yml
```

It is a clean no-op on a healthy cluster, so it is safe to run anytime.

**Manual**, if doing it by hand — roll one node at a time, leader last,
waiting for quorum (`microk8s status` shows 3 datastore masters) between
each so you never drop more than one voter:

```sh
ssh <node> 'sudo systemctl restart snap.microk8s.daemon-k8s-dqlite'
# wait ~30s; re-run the cache-vs-quorum check on <node> until lag→0; then next node
```

After recovery the stuck Deployment reconciles on its own; pods already
scheduled keep running regardless. Note a freeze can take dependent
workloads down with it (e.g. a pod that hard-exits when its backing service
is briefly unreachable) — those recover once their dependency is back.

## Note

This is a workaround for a bug with no released fix. Track PR #365; when it
ships in a 1.35.x build, upgrade to that and the watchdog becomes a
defense-in-depth safety net rather than a routine necessity.
