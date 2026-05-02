# 03 — Pre-drain hand-off in update + rebuild playbooks

## Goal

Stop a bare `kubectl drain` from evicting Keycloak and keycloak-db
mid-update or mid-rebuild. Both workloads have a controlled hand-off
(`rollout restart` against the still-uncordoned cluster) before the drain
runs, so by the time the drain fires, nothing critical lives on the
target node.

Decisions taken with the operator:

- Apply to **both** `update-k8s.yml` and `rebuild-k8s.yml`. Keycloak /
  keycloak-db are exposed during rebuild drains too; Phase 4c hits this
  path four times.
- Patch order is the natural Ansible serial order — **no sort by
  labeled-pod count**. Simpler, no inventory rewrites at runtime, accepts
  that the first labeled-pod node gets less surge headroom than the last.
  The risk is small and the optimization is anti-Ansible-shape.
- HelmCharts label change is part of this plan, landing as a separate
  commit in `/work/HelmCharts`.

## Steps

### Shared task file

`ansible/playbooks/tasks/pre-drain-handoff.yml`:

Inputs (passed via `vars:` on the importer):

- `pre_drain_node` — host being prepped (default: `inventory_hostname`).
- `pre_drain_primary` — host that runs kubectl (default:
  `microk8s_primary_host`).

Body:

1. Cordon the node (delegate_to primary).
2. Find labeled deployments with running pods on this node — Pod →
   ReplicaSet → Deployment walk:

   ```
   microk8s kubectl get pods -A \
     -l iac.webathome.org/pre-drain=true \
     --field-selector spec.nodeName=$NODE,status.phase=Running -o json
   ```

   Parse via `json_query` or by piping through `jq` in a `command` task,
   register the resulting `[{namespace, name}, …]` list as a fact.
3. If the list is empty, skip steps 4–5.
4. `microk8s kubectl rollout restart deploy/<name> -n <ns>` looped over
   the list (sequential; the loop body fires the restart synchronously
   but doesn't wait — wait happens in step 5).
5. `microk8s kubectl rollout status deploy/<name> -n <ns> --timeout=5m`
   looped over the same list. **Fail the play on non-zero exit** — never
   drain when a labeled workload is unhealthy on its surge target.

All steps `delegate_to: "{{ pre_drain_primary }}"` and gated on
`_cluster_peer_count | int > 1` (mirroring the existing drain task in
`update-k8s.yml`). On a single-node cluster the hand-off and the drain
are both no-ops.

### Wire into `update-k8s.yml`

- Replace the existing `Drain {{ inventory_hostname }}` task with:
  - `import_tasks: tasks/pre-drain-handoff.yml`
  - then the existing drain task (unchanged) — the cordon now happens
    inside the hand-off, but `kubectl drain` cordons implicitly anyway,
    so the duplication is harmless.
- Add a final `kubectl uncordon` step at the end of the per-host loop
  (mirrors the spec's step 7 — closes the cordon the hand-off opened).
- The peer-count gate already wraps the drain; the hand-off needs the
  same gate. Compute `_cluster_peer_count` once at the start of the
  per-host block and reuse.

### Wire into `rebuild-k8s.yml`

- Same shape — `import_tasks: tasks/pre-drain-handoff.yml` → drain →
  rebuild flow takes over.
- No final uncordon: the rebuild destroys the node, the new node comes
  back fresh and joins.
- Verify the existing rebuild flow's drain task expects the node to be
  cordoned at entry (the hand-off already cordoned it) — adjust if the
  current task assumes an uncordoned node.

### HelmCharts label change

Repo: `/work/HelmCharts`. Separate commit, separate PR if relevant.

For each of the keycloak and keycloak-db Deployments:

- Add `iac.webathome.org/pre-drain: "true"` to:
  - `metadata.labels` (so `kubectl get deploy -l ...` enumerates opt-ins).
  - `spec.template.metadata.labels` (so `kubectl get pods -l ...` finds
    running pods — this is the query the hand-off uses).
- Confirm Deployment strategies match the spec's assumptions:
  - keycloak: `RollingUpdate` with `maxSurge: 1, maxUnavailable: 0`.
    Brief two-pod window during surge is acceptable.
  - keycloak-db: `Recreate` (RWO PVC, single Postgres). ~30s outage
    during the controlled swap is the cost.
- Verify the dnsmasq `PodDisruptionBudget` (`charts/dnsmasq/templates/dns-pdb.yaml`)
  exists with `minAvailable: 1`. dnsmasq doesn't use this hand-off
  (StatefulSet, not Deployment, plus client-side LB-IP failover), but
  the PDB is the equivalent guardrail on its side. If missing, add — but
  that's a separate concern from this plan.

### Runbook + docs

`docs/runbooks/k8s-rebuild.md`:

- Note that pre-drain hand-off runs automatically for labeled workloads;
  workloads opt in via the `iac.webathome.org/pre-drain` label.
- Document the label as a forward contract: future Deployments that need
  controlled hand-off (single replica, RWO PVC, etc.) opt in by setting
  it on both `metadata.labels` and `spec.template.metadata.labels`.
- Call out the caveats:
  - Concurrent Keycloak pods during surge: ~60s, no sticky sessions on
    the in-house ingress, mid-login requests can re-auth. Acceptable
    today; revisit if it becomes noticeable.
  - keycloak-db: ~30s outage during the controlled swap. Better than
    mid-drain.
  - Rollout-status timeout: 5 minutes. If a rollout doesn't reach Ready,
    the play aborts before draining — operator fixes the workload before
    retrying.
  - DaemonSets and StatefulSets must **not** carry the label. The Pod →
    ReplicaSet → Deployment walk silently ignores them, so labeling them
    is dormant config.

## Verification

- `update-k8s.yml --check --diff` against `k8s_dev` (single-node) — the
  peer-count gate should make every hand-off step skip cleanly. No
  `changed=N>0` from the new tasks.
- `update-k8s.yml --check --diff` against `k8s_prd` — confirm the
  hand-off enumerates the labeled deployments correctly per node and
  fires the rollouts on the node currently hosting them.
- After Phase 4c lands: `rebuild-k8s.yml` exercises the same path during
  each VM rebuild. Operator confirms Keycloak stays serving across the
  rebuild window (modulo the documented surge / Recreate caveats).

## Caveats

- The hand-off depends on the HelmCharts label change being deployed
  before the playbook runs. Order of operations: land the Ansible side
  → land the HelmCharts side → next time the operator runs `update-k8s`
  or `rebuild-k8s`, the hand-off fires for real. If HelmCharts hasn't
  rolled, the playbook degrades to a plain drain (safe, but the feature
  is dormant).
- `rollout restart` is the move's hammer — it applies even if the
  Deployment had no spec drift. Acceptable: the drain would have
  restarted the pods anyway, and `rollout restart` puts the restart at
  a controlled moment.

## Commits

1. `/work/Ansible`: shared task file + integration in `update-k8s.yml` +
   integration in `rebuild-k8s.yml` + runbook update. One commit; the
   playbook integrations don't make sense without the task file, and
   vice versa.
2. `/work/HelmCharts`: keycloak + keycloak-db Deployment label addition.
   Separate repo, separate commit.
