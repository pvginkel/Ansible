# 07 — Pre-drain hand-off readiness check

## Symptom

During Phase 4c pre-flight on `srvk8ss2`, the hand-off task
`Wait for each labeled Deployment to be Ready post-restart`
(`ansible/playbooks/tasks/pre-drain-handoff.yml`) reported `ok` for both
`keycloak/keycloak` and `keycloak/keycloak-db` while the Deployments
were still showing not-Ready — the new pod had not yet passed its
readiness probe. The play would have proceeded to drain on top of an
unhealthy surge target if the previous step (the bare-Pod failure) had
not aborted it first.

The task uses `microk8s kubectl rollout status deploy/<name>
--timeout=5m`, which is the Deployment-level check, so in theory it
should block until the rollout completes. In practice it returned 0
early. Needs investigation before we trust the hand-off in anger.

## Direction (to flesh out when we pick this up)

- Reproduce with verbose output and `kubectl get deploy <name> -o yaml`
  snapshots taken at the moment `rollout status` returns, to confirm
  what `observedGeneration`, `replicas`, `updatedReplicas`,
  `readyReplicas`, and `availableReplicas` look like at exit.
- Likely candidates: a quirk of `rollout restart` + `RollingUpdate`
  (`maxSurge: 1, maxUnavailable: 0`) where the old pod still counts as
  Available and the rollout-status criteria pass before the new pod is
  Ready; or a quirk of `Recreate` where the brief `replicas=0` window
  satisfies all the loop's checks.
- Replace or supplement `rollout status` with an explicit Deployment
  state check — e.g. poll `kubectl get deploy <name> -o jsonpath` until
  `.status.observedGeneration == .metadata.generation` AND
  `.status.readyReplicas == .spec.replicas` AND
  `.status.updatedReplicas == .spec.replicas`. Same 5-minute timeout,
  fail-loud on miss.
- Verify against both strategies (`RollingUpdate maxSurge:1/maxUn:0`
  for keycloak; `Recreate` for keycloak-db).

## Out of scope here

The plan that introduced the hand-off is `03-pre-drain-handoff.md`.
This is a follow-up: the hand-off itself works, but its readiness gate
isn't tight enough.

## Status

Not a blocker for Phase 4c — the rebuild's own drain step would still
fail loudly if a surge target were genuinely unhealthy (drain refuses
PDB-violating evictions; the cordon stays in place). But a tighter
gate stops us from depending on that fallback.
