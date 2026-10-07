# Kyverno: memory requests at pod creation

Kyverno runs on prd for one job: it sets each pod's memory requests when the pod is created, from a
table that a daily CronJob computes from Prometheus (AnsibleSpecs
[argo-cd D65](../../../AnsibleSpecs/argo-cd/decisions.md) as amended 2026-10-06). Read this to see
what a pod got and why, to refresh the table by hand, when pod creation is refused because Kyverno
is down, and before upgrading or removing the app.

## Conventions

- Reads work with the default kubeconfig. Every write is the operator's keystroke and takes the
  prd-write kubeconfig:

  ```sh
  KC="--kubeconfig $HOME/.kube/config-prd-write --context prd"
  ```

- The commands run from KubeCoder through `cexec iac`. On a control-plane node they are
  `sudo microk8s kubectl …`, without `$KC`.

## Facts

| Item | Value |
| --- | --- |
| Application, namespace | `kyverno-prd`: app `kyverno`, stage `prd`, in ArgoCDDeploy `releases/values.yaml` |
| Deploy repo | KyvernoDeploy: the upstream chart `kyverno` 3.9.1 (Kyverno v1.19.1) with `config/prd/values.yaml`, and the companion `chart/` |
| Admission controller | Deployment `kyverno-admission-controller`: three replicas, one per node on srvk8s1–3, PDB `minAvailable: 1`; Service `kyverno-prd-svc`. No other Kyverno controller runs |
| Pod webhook | MutatingWebhookConfiguration `kyverno-resource-mutating-webhook-cfg`, entry `mpol.validate.kyverno.svc-fail`, `failurePolicy: Fail`, owned by ClusterRole `kyverno-prd:webhook` |
| Policy | MutatingPolicy `memory-requests` and GlobalContextEntry `memory-requests`, both cluster-scoped: KyvernoDeploy `chart/templates/memory-requests-policy.yaml` |
| Snapshot | CronJob `memory-snapshot` in `kyverno-prd`, daily at 04:15 Europe/Amsterdam, running `chart/files/memory_snapshot.py` from ConfigMap `memory-snapshot`: KyvernoDeploy `chart/templates/memory-snapshot.yaml` |
| Table | ConfigMap `memory-requests` in `kyverno-prd`, one row per container: `<namespace>_<workload>_<container>: <n>Mi` |
| Tests | KyvernoDeploy `kc project test`: `tests/kyverno-test.py` runs the policy's cases with `kyverno test`, `tests/test_memory_snapshot.py` the script's; both derive workloads from the examples in `tests/workload-examples.yaml` |

## What it does to pod creation

On every pod create outside the skips below, the API server sends the pod to Kyverno before storing
it. The policy:

- **Derives the pod's workload** from its controller owner: a ReplicaSet's name without its
  `pod-template-hash` (the Deployment), a StatefulSet's or DaemonSet's name, a Job's name without a
  trailing `-<digits>` (a CronJob, or `<job>` for the library's validation Job `<job>-<build>`), a
  CloudNativePG Cluster's name. An ownerless pod in `jenkins-prd` is a build agent named
  `<job>-<build>-…`, and its workload is the job. Any other pod has none.
- **Sets each container's memory request**, init containers included, to the row
  `<namespace>_<workload>_<container>`, capped at the container's memory limit. It does so whether
  the row is lower or higher than the request the pod arrived with. Limits and CPU are never
  touched.
- **Admits the pod untouched where data is missing**: the table has no `data` yet, the
  GlobalContextEntry has not loaded it on the replica that answers, there is no row, or the pod has
  no workload. Kubernetes defaults a missing request to the limit before admission, so such a
  container has its limit as its request, or no request at all.

It acts at creation only. A running pod is never resized or restarted; a workload gets a new number
with its next pod.

The skips are selectors in the webhook registration itself, so the API server never sends these
pods to Kyverno and creates them while Kyverno is down:

| Skipped | Why |
| --- | --- |
| `kube-system`, `metallb-system` | Ansible books their requests (`microk8s` role) |
| `argocd-prd`, `argocd-hooks` | Argo CD and its hooks stay able to repair Kyverno from Git |
| `kyverno-prd` | Kyverno's own pods and the snapshot job carry requests committed in KyvernoDeploy |
| pods labelled `app.kubernetes.io/managed-by: kubecoder` | KubeCoder's environment pods, booked at 0 on purpose |

Every other namespace is covered, the `-dev` stages' included. Under `failurePolicy: Fail`, a pod
create in a covered namespace that no Kyverno replica answers is refused: see
[Break-glass](#break-glass).

## The snapshot

The job queries `prometheus-prd` for 28 days of `container_memory_working_set_bytes` at a 300 s
step, pools every pod of a workload, and writes each container's p90 in whole MiB, rounded up. It
replaces the table's `data` whole, so a workload gone from the window loses its row. Kyverno watches
the ConfigMap, so the next pod gets the new table without a restart.

The job leaves the table as it was, and fails, when Prometheus fails, when it returns no rows, or
when it returns fewer than half the rows the table holds. The Job retries twice. The chart creates
the table without `data`, so no Argo CD sync writes it.

```sh
cexec iac kubectl -n kyverno-prd get cronjob,jobs                       # the last runs
cexec iac kubectl -n kyverno-prd logs job/<job>                         # a run's result
cexec iac kubectl -n kyverno-prd get configmap memory-requests -o json | jq '.data | length'
```

A run ends with `table kyverno-prd/memory-requests: <n> rows, <n> new, <n> gone`, or with
`table kyverno-prd/memory-requests left as it is: <reason>` and exit 1 when Prometheus returned no
rows or too few. A run that Prometheus or the API server fails ends in a Python traceback instead,
with the table left as it is too.

### Refreshing it by hand

```sh
JOB=memory-snapshot-$(date +%Y%m%d-%H%M)
cexec iac kubectl $KC -n kyverno-prd create job --from=cronjob/memory-snapshot $JOB
cexec iac kubectl -n kyverno-prd logs -f job/$JOB
```

### Making it take a table its guard refuses

Only the "fewer than half" refusal can be overridden; an empty result is never written. The guard
compares against the table's `data`, so save the table, remove its `data` and run the job at once:

```sh
cexec iac kubectl -n kyverno-prd get configmap memory-requests -o json > memory-requests-$(date +%F).json
cexec iac kubectl $KC -n kyverno-prd patch configmap memory-requests --type=json \
  -p '[{"op":"remove","path":"/data"}]'
```

Then run the three lines above. Until the run writes, every covered pod is admitted untouched. The
workloads the new table lacks get no request at their next pod, until a later run measures them. To
put the saved table back:

```sh
cexec iac kubectl $KC -n kyverno-prd patch configmap memory-requests --type=merge \
  -p "$(jq -c '{data}' memory-requests-<date>.json)"
```

## Which request a pod got, and why

The pod's requests and limits, then its controller owner:

```sh
NS=<namespace> POD=<pod>
cexec iac kubectl -n $NS get pod $POD -o jsonpath='{range .spec.initContainers[*]}{.name} (init): {.resources.requests.memory}, limit {.resources.limits.memory}{"\n"}{end}{range .spec.containers[*]}{.name}: {.resources.requests.memory}, limit {.resources.limits.memory}{"\n"}{end}'
cexec iac kubectl -n $NS get pod $POD -o jsonpath='{.metadata.creationTimestamp} {.metadata.ownerReferences[?(@.controller==true)].kind}/{.metadata.ownerReferences[?(@.controller==true)].name}{"\n"}'
```

Derive the workload from the owner as [above](#what-it-does-to-pod-creation), then read its rows:

```sh
WL=<workload>
cexec iac kubectl -n kyverno-prd get configmap memory-requests -o json \
  | jq -r --arg p "${NS}_${WL}_" '.data // {} | to_entries[] | select(.key | startswith($p)) | "\(.key) \(.value)"'
```

| The container shows | Because |
| --- | --- |
| the row | Kyverno set it |
| its limit, below the row | the request is capped at the limit |
| a different number than the row | the pod is older than the table's last write; its next pod gets the row |
| what its manifest says, its limit, or nothing | no row (a new workload, or none of its samples in the last 28 days), no workload, a skipped namespace or label, no table loaded, or the pod was created while the registration was deleted ([Break-glass](#break-glass)) |

Kyverno records no event for a pod it sized or left alone
(`--omitEvents=PolicyApplied,PolicySkipped`).
Whether the policy and its table are loaded, and the admission controller's errors:

```sh
cexec iac kubectl get mutatingpolicies.policies.kyverno.io memory-requests      # READY true
cexec iac kubectl describe globalcontextentries.kyverno.io memory-requests      # conditions; events on a failed load
cexec iac kubectl -n kyverno-prd logs -l app.kubernetes.io/component=admission-controller --prefix --tail=50
```

## Break-glass

When to use it: pods in covered namespaces are not created, and the refusal names Kyverno's
webhook. A Deployment rolls nothing out; its ReplicaSet, a Job or a StatefulSet shows
`FailedCreate` events such as:

```
Error creating: Internal error occurred: failed calling webhook "mpol.validate.kyverno.svc-fail": failed to call webhook: Post "https://kyverno-prd-svc.kyverno-prd.svc:443/…": …
```

A server-side dry run in `development`, which the default kubeconfig may write, goes through
admission and creates nothing. It prints `pod/kyverno-probe` when admitted, and the refusal when
not:

```sh
cexec iac kubectl -n development run kyverno-probe --image=busybox --restart=Never --dry-run=server -o name
```

A refusal that reads `admission webhook "mpol.validate.kyverno.svc-fail" denied the request` is
Kyverno answering with an error from the policy. The break-glass restores pod creation then too,
but the fix is in the policy. What Kyverno itself is doing:

```sh
cexec iac kubectl -n kyverno-prd get pods -l app.kubernetes.io/component=admission-controller -o wide
```

**Restore pod creation** by scaling Kyverno to zero, then deleting every webhook registration it
owns. Scale first, every time. A replica that leads rewrites the registrations every 10 seconds
(Kyverno v1.19.1), and a crash-looping replica re-creates them on its next restart before it dies
again, so a deletion holds only while no replica runs. A replica that shuts down cleanly with the
Deployment at zero deletes the registrations itself; a crashed one leaves them, and the label
delete takes whatever still stands. The label covers the pod webhook and the `failurePolicy: Fail`
webhooks that guard Kyverno's own objects: `kyverno-policy-validating-webhook-cfg` on its policies
and `kyverno-global-context-validating-webhook-cfg` on its GlobalContextEntries. A KyvernoDeploy
sync applies both kinds, so it would be refused while they stand.

```sh
cexec iac kubectl $KC -n kyverno-prd scale deployment kyverno-admission-controller --replicas=0
cexec iac kubectl -n kyverno-prd wait --for=delete pod -l app.kubernetes.io/component=admission-controller --timeout=2m
cexec iac kubectl $KC delete mutatingwebhookconfiguration,validatingwebhookconfiguration -l webhook.kyverno.io/managed-by=kyverno
```

A pod that hangs in Terminating on a dead node doesn't run; go on to the delete when the wait
times out. When the API token or the apiserver VIP is the broken thing, do it on a control-plane
node, by IP if DNS is down too ([cold-boot.md](cold-boot.md#break-glass), "SSH by IP"):

```sh
ssh ansible@srvk8s1 sudo microk8s kubectl -n kyverno-prd scale deployment kyverno-admission-controller --replicas=0
ssh ansible@srvk8s1 sudo microk8s kubectl delete mutatingwebhookconfiguration,validatingwebhookconfiguration -l webhook.kyverno.io/managed-by=kyverno
```

Then run the probe again: it is admitted.

**Bring Kyverno back.** With its registrations gone, nothing of Kyverno's refuses an apply, so a
fix pushed to KyvernoDeploy syncs as usual. Kyverno doesn't come back by itself after the
break-glass: with `selfHeal` off, Argo leaves the Deployment at zero until it is scaled back or
KyvernoDeploy syncs. Pods in `kyverno-prd` are never refused, so scaling up always works. Once a
replica leads, it registers the webhooks again; check the pod entry and its owner:

```sh
cexec iac kubectl $KC -n kyverno-prd scale deployment kyverno-admission-controller --replicas=3
cexec iac kubectl get mutatingwebhookconfiguration kyverno-resource-mutating-webhook-cfg \
  -o custom-columns='WEBHOOKS:.webhooks[*].name,OWNER:.metadata.ownerReferences[*].name'
```

Pods created while the registration was gone kept the request they arrived with: none, or their
limit. Each gets the table's number with its next pod. List them, and `kubectl rollout restart`
the workloads that should not wait:

```sh
SINCE=<UTC time of the deletion, as 2026-10-06T10:00:00Z>
cexec iac kubectl get pods -A -o json \
  | jq -r --arg t "$SINCE" '.items[] | select(.metadata.creationTimestamp >= $t) | "\(.metadata.namespace)/\(.metadata.name)"'
```

### Drilling it

With Kyverno healthy, at a quiet moment. Scaling to zero alone leaves nothing to break: on the way
down Kyverno deletes its own registrations. The drill puts the registration back once the pods are
gone, the state a crashed or unreachable Kyverno leaves. Between steps 2 and 3, every pod create in
a covered namespace is refused.

1. Save the registration, scale the admission controller to zero and wait for its pods to go. The
   registration is gone.
2. Create the saved copy. The probe is refused.
3. Delete Kyverno's registrations by label, Restore's last command. The probe is admitted.
4. Scale back to three. Once a replica is Ready, the registration is back with its entry and its
   owner, and the probe is admitted through Kyverno.

```sh
cexec iac kubectl get mutatingwebhookconfiguration kyverno-resource-mutating-webhook-cfg -o json \
  | jq '.metadata |= {name, labels, annotations, ownerReferences}' > kyverno-webhook.json
cexec iac kubectl $KC -n kyverno-prd scale deployment kyverno-admission-controller --replicas=0
cexec iac kubectl -n kyverno-prd wait --for=delete pod -l app.kubernetes.io/component=admission-controller --timeout=5m
cexec iac kubectl get mutatingwebhookconfiguration kyverno-resource-mutating-webhook-cfg   # NotFound
cexec iac kubectl $KC create -f kyverno-webhook.json
```

## Upgrading and removing the app

**Upgrading.** Bump the chart as KyvernoDeploy's README says. A version that brings a new
cluster-scoped kind also needs ArgoCDDeploy's `releases` AppProject whitelist and the
`SYNCED_CLUSTER_KINDS` table in its `tests/render-chart.py`, edited together. After the sync, check
that Kyverno still sets the owner that removal depends on:

```sh
cexec iac kubectl get mutatingwebhookconfigurations,validatingwebhookconfigurations \
  -o custom-columns='NAME:.metadata.name,OWNER:.metadata.ownerReferences[*].name' | grep kyverno
```

Every `kyverno-resource-*`, `kyverno-policy-*` and `kyverno-verify-*` row shows
`kyverno-prd:webhook`, and the exception and global-context rows show `<none>`. Kyverno v1.19.1
sets that owner to the ClusterRole named `*:webhook` and labelled
`app.kubernetes.io/component: kyverno`. A registration without the owner outlives the app.

**Removing.** Unregister the app as
[argocd.md](argocd.md#registering-undeploying-and-unregistering-an-app) says. The chart's
pre-delete webhook cleanup is off: Argo CD runs every pre-delete hook at once, and the chart's
scale-down looks in namespace `kyverno`, so the cleanup raced a live admission controller.
Instead the companion's ClusterRole `kyverno-prd:webhook` (KyvernoDeploy
`chart/templates/webhook-owner.yaml`, sync wave −1) owns the registrations. Argo deletes the
admission controller's Deployment and its pods before wave −1, and the garbage collector then
deletes what the ClusterRole owned. Between the last admission-controller pod's exit and that,
covered pod creates are refused.

What stays: Argo CD never deletes a CRD with its Application, so Kyverno's 22 CRDs stay. The
three owner-less validating registrations for policy exceptions and global context entries, all
`Fail`, may stay too. An admission-controller pod that shuts down while its Deployment is being
deleted deletes every registration Kyverno manages, these three included, but only if its
ClusterRoleBinding is still there to allow it, so a removal can leave all, some or none of them.
None matches pods, but those that stay refuse creating those kinds, a reinstall's first sync
included, until an admission controller answers. If Kyverno is not coming back, delete them all:

```sh
cexec iac kubectl get mutatingwebhookconfigurations,validatingwebhookconfigurations | grep kyverno
cexec iac kubectl $KC delete validatingwebhookconfiguration kyverno-exception-validating-webhook-cfg \
  kyverno-cel-exception-validating-webhook-cfg kyverno-global-context-validating-webhook-cfg
cexec iac kubectl get crd -o name | grep -E 'kyverno\.io$|wgpolicyk8s\.io$' | xargs cexec iac kubectl $KC delete
```

The first line lists whichever of those three are still there; the delete reports `NotFound` for
one already gone and deletes the others. A `kyverno-resource-mutating-webhook-cfg` still listed
there had lost its owner, and refuses covered pods: delete it as in [Break-glass](#break-glass).
