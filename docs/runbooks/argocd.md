# Argo CD operator runbook

Day-to-day operation of the Argo CD instance on the prd cluster: reading its
state without the CLI, diagnosing a failed sync, webhooks, rotating its tokens,
upgrading it, getting in when SSO is broken, registering and removing an app,
and rebuilding it from nothing. Read this when a sync fails, a token or secret
changes, or Argo itself needs an upgrade or a bootstrap.

Design context: the `argo-cd/` document set in AnsibleSpecs —
[`brief.md`](../../../AnsibleSpecs/argo-cd/brief.md),
[`design.md`](../../../AnsibleSpecs/argo-cd/design.md),
[`decisions.md`](../../../AnsibleSpecs/argo-cd/decisions.md) (cited as `Dn`),
[`phases.md`](../../../AnsibleSpecs/argo-cd/phases.md). The bootstrap as it
actually ran and the Phase A proof drill are recorded in slice 009's
[`close-out.md`](../../../AnsibleSpecs/slices/completed/009_argocd_standup/close-out.md)
(A1–A3).

## Conventions

- The operator's keystroke applies: every sync, every bootstrap command, every
  `bao kv put`, every deletion. Claude prepares and reads.
- There is no `argocd` CLI in the `iac` sidecar and none is needed. Everything
  below is `kubectl` with the prd-write kubeconfig; the read-only default
  kubeconfig cannot list `argoproj.io` kinds. Shorthand used throughout:

  ```sh
  KC="--kubeconfig $HOME/.kube/config-prd-write --context prd"
  cexec iac kubectl $KC get applications.argoproj.io -A
  ```

- Polling is off (D6). Argo learns about a push only through the webhook; no
  webhook means no refresh, ever.
- Argo's own Application never auto-syncs (D3). Every Argo upgrade is a manual
  sync at a moment the operator picks.

## Facts

| Item | Value |
| --- | --- |
| Namespaces | `argocd-prd` (Argo, the webhook relay), `argocd-hooks` (PreSync Jobs, the `tf-presync` ServiceAccount, `argocd-hook-credentials`) |
| Helm release, Application, AppProject | `argocd-prd`, `argocd-prd`, `releases` |
| UI | `https://argocd.home` — Keycloak SSO (realm `homelab`, client `argocd`); the bare `https://argocd` is broken |
| Deploy repo | `ArgoCDDeploy`: exact `argo-cd` pin in `chart/Chart.yaml`, stage values in `config/prd/values.yaml` |
| Registry | HelmCharts `configs/prd/<app>/<stage>/release.yaml` carrying `reconciler: argo-cd` |
| ApplicationSets | `releases-local`, `releases-upstream` (`missingkey=error`: one malformed entry fails the whole set) |
| Webhook edge | `https://deploy-hooks.webathome.org/api/webhook` → relay (2 replicas) → argocd-server and the applicationset-controller |
| Hook image | `registry:5000/argocd-hook:<n>` from ArgoCDTools; default pin in the `homelab-shared` library chart |
| Terraform state | `pvginkel/TerraformState`, `argocd/<repo>/<stage>/terraform.tfstate`, sops/age |
| Notifications | Alertmanager `prometheus-prd-alertmanager.prometheus-prd:9093`, delivered to Telegram; `ArgoCDSyncFailed` (critical, with sound), `ArgoCDHealthDegraded` (warning, silent) |

Every credential arrives through ESO from OpenBao (`kv/` mount), refreshed hourly:

| ExternalSecret | Leaf and property | Reader |
| --- | --- | --- |
| `argocd-prd/argocd-repo-creds-github` | `eso/prd/argocd/prd/git#token` | Argo's own repo clones (classic PAT, `repo`) |
| `argocd-prd/argocd-webhook` | `eso/prd/argocd/prd/webhook#github_secret` | both receivers and the relay; the same value GitHub holds on every hook |
| `argocd-prd/argocd-oidc` | `eso/prd/argocd/prd/oidc#client_secret` | SSO |
| `argocd-hooks/argocd-hook-credentials` | `eso/prd/argocd-hooks/git#token` plus nine more leaves, 23 keys — `webhook#github_secret` above among them, as `TF_VAR_github_webhook_secret` | the PreSync hook: its clone, state pushes, provider credentials, the secret a deploy repo's webhook is signed with |

The two PATs are deliberately separate and rotate independently. Regenerating a
classic PAT on GitHub invalidates its old value, so a PAT backing more than one
leaf has to be rewritten everywhere it lives.

## Reading Argo without the CLI

```sh
# Every Application, sync and health
cexec iac kubectl $KC get applications.argoproj.io -A
# The last operation: phase and message
cexec iac kubectl $KC get application -n argocd-prd <app> \
  -o jsonpath='{.status.operationState.phase}{"\n"}{.status.operationState.message}{"\n"}'
# Per-resource results of that operation (hooks included)
cexec iac kubectl $KC get application -n argocd-prd <app> \
  -o jsonpath='{range .status.operationState.syncResult.resources[*]}{.kind}/{.name} {.hookPhase}{.status}: {.message}{"\n"}{end}'
# The hook Jobs and their logs
cexec iac kubectl $KC get jobs,pods -n argocd-hooks
cexec iac kubectl $KC logs -n argocd-hooks job/<tf-presync-...>
# Controller, repo-server, notifications, relay
cexec iac kubectl $KC logs -n argocd-prd argocd-prd-application-controller-0 --since=15m | grep <app>
cexec iac kubectl $KC logs -n argocd-prd deploy/argocd-prd-repo-server --since=15m
cexec iac kubectl $KC logs -n argocd-prd deploy/argocd-prd-notifications-controller --since=15m | grep <app>
cexec iac kubectl $KC get pods -n argocd-prd -o name | grep webhook-relay   # then `logs` per pod
```

In the UI the apply error lives on the **operation**, not on the resource: click
the *Last Sync* tile at the top of the Application and read the *Result*
section. The resource tree only shows the refused object as *Missing*.

## Diagnosing a failed sync

1. **Operation `Failed`, the only failed result a `Job/tf-presync-…` with "Job
   has reached the specified backoff limit".** The hook failed and nothing was
   applied. Read the Job's log (the Job name carries the revision). Seen so far:
   - `remote: Invalid username or token` on the clone — the hook's PAT at
     `eso/prd/argocd-hooks/git` is no longer accepted. Rotate it (below).
   - `Error: Resource precondition failed` or any other Terraform error — the
     app's own Terraform; the message names the file and line.
   - a Terraform error on a namespace, forbidden or "already exists" — the
     app's Terraform manages its namespace. It must not: Argo applies the chart's
     Namespace before the hook runs, and the hook's ClusterRole grants no
     `namespaces`.
2. **A resource marked `SyncFailed`.** The API server refused that object; its
   message is on the resource result. A sync-phase failure is **not atomic**:
   the valid objects in the same wave were applied anyway.
3. **`ComparisonError` or no manifests.** The repo-server. A `helm template`
   error reading "found in Chart.yaml, but missing in charts/ directory"
   followed by `helm repo add` and `helm dependency build` is Argo's normal
   first attempt, not a failure.
4. **The Application does not appear, or does not refresh after a push.** The
   webhook, or the applicationset-controller's one-shot handler — both below.

Hook Jobs persist for the app's lifetime, one per sync (`backoffLimit: 0`, so a
failed hook is exactly one pod), and are removed with the Application.

## Webhooks

A deploy repo whose Terraform carries D39's `github_repository_webhook` gets
its GitHub webhook from the first sync of the one stage whose tfvars set
`manage_webhook = true` — dev, for KubeCoderDeploy — signed with the hook's
`TF_VAR_github_webhook_secret`. Whether the hook's classic PAT can create it is
unconfirmed until that first apply (D41). Never add one by hand to such a repo:
the apply's create then fails on GitHub's hook-already-exists.

Any other deploy repo needs its webhook made by hand: payload URL
`https://deploy-hooks.webathome.org/api/webhook`, content type
`application/json`, the shared secret from
`eso/prd/argocd/prd/webhook#github_secret`, just the push event. The pod's
GitHub token can list hooks (`gh api repos/pvginkel/<repo>/hooks`) but not
create them, so creation is a GitHub UI keystroke.

A delivery that worked leaves three traces: the relay logs `delivery <id>
event=push: both receivers accepted`; argocd-server logs `Received push event
repo: … refreshing app from webhook`; the Application's `status.sync.revision`
moves within about ten seconds. GitHub's *Recent Deliveries* shows the relay's
response, and a `502` names the dead leg.

Without a webhook, refresh by hand — the UI's *Refresh* button, or:

```sh
cexec iac kubectl $KC annotate application -n argocd-prd <app> argocd.argoproj.io/refresh=hard --overwrite
```

A hard refresh takes a few seconds; reading `status.sync.revision` immediately
returns the previous value.

## Restarting the applicationset-controller

```sh
cexec iac kubectl $KC -n argocd-prd rollout restart deploy/argocd-prd-applicationset-controller
```

When: after every bootstrap, after the webhook secret changes, and whenever its
log shows `failed to create webhook handler` or `error retrieving Git files: …
connection refused` with no Applications generated. It builds its GitHub webhook
handler once at startup from a one-shot settings read and subscribes to nothing,
and with polling off a failed generation attempt is never retried. argocd-server
is unaffected — it watches its settings.

## Rotating a token or secret

1. Write the new value to its leaf, from stdin so it never lands in a history:

   ```sh
   printf %s "$VALUE" | bao kv put -mount=kv eso/prd/argocd-hooks/git token=-
   ```

2. ESO refreshes within the hour. To force it:

   ```sh
   cexec iac kubectl $KC annotate externalsecret -n <namespace> <name> force-sync=$(date +%s) --overwrite
   ```

   Both leaf-to-ExternalSecret pairs are in the table above.
3. Argo reads its repo-creds Secret live; the hook reads its Secret at Job
   start, so the next sync uses the new value. To check a token without reading
   it, run a throwaway pod on the hook image with `envFrom` the Secret and print
   only the status code of `GET https://api.github.com/user` (401 vs 200), then
   let `--rm` delete it.

## Upgrading Argo CD

1. In ArgoCDDeploy, bump the exact `argo-cd` version in `chart/Chart.yaml`,
   rebuild `Chart.lock`, run the repo's render gate, push.
2. The webhook refreshes `argocd-prd`; it goes OutOfSync. Review the diff in the
   UI. The CRDs sync server-side (`ServerSideApply=true` on all three).
3. Sync by hand at a chosen moment (D3). The controller and repo-server restart
   mid-sync, and every Application pauses with them.
4. Verify: `argocd-prd` Synced and Healthy, every pod Running, the full
   Application list back, and a webhook delivery accepted by both receivers. If
   Applications stop regenerating, restart the applicationset-controller.

## Break-glass: the local admin account (D9)

For when Keycloak, the `argocd` client or the OIDC leaf is broken. `admin` is
enabled in `argocd-cm`; the chart minted its initial password into
`argocd-initial-admin-secret` at install. Reading it is a credential read and the
operator's:

```sh
cexec iac kubectl $KC -n argocd-prd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
```

Log in at `https://argocd.home` with the username/password form beneath the SSO
button. If the initial secret is gone or the password was changed, set a new
one the upstream way: a bcrypt hash into `argocd-secret` under `admin.password`
with `admin.passwordMtime` set to now. **Not yet exercised** — a Phase A.5 item
still open; try it once at a quiet moment.

The SSO side maps `preferred_username` `pvginkel@gmail.com` to `role:admin` in
`argocd-rbac-cm` (the realm uses email as username); `policy.default` is empty,
so any other identity gets nothing at all.

## Registering, undeploying and unregistering an app

The entry, at HelmCharts `configs/prd/<app>/<stage>/release.yaml` — the path
names the Application and its namespace `<app>-<stage>`:

```yaml
reconciler: argo-cd
deployed: true
autoSync: false
repo: https://github.com/pvginkel/<DeployRepo>
targetRevision: main
```

No `chart:` key. HelmCharts' suite (`kc project test`) gates the shape, and the
pipeline skips the release. Push; the ApplicationSet regenerates within seconds
and the Application appears OutOfSync. The first sync is manual.

What a sync does, in order: Argo applies the chart's `sync-wave: "-1"` Namespace
during its dry-run pass; the PreSync Job runs in `argocd-hooks` (clone at the
synced SHA → terraform-backend-git → `terraform apply` with the stage tfvars →
PV reattach); then the Sync waves. The app's Terraform takes `var.namespace` as
given and never creates it.

`autoSync: true` generates `syncPolicy.automated` (`prune: true`, `selfHeal:
false`) within about ten seconds and Argo syncs on its own; flipping it back
removes the policy.

**Undeploy** is `deployed: false`: the ApplicationSet deletes the Application and
the resources finalizer cascades — workloads, the `Prune=false` Namespace and
the hook Jobs were all gone within 45 seconds on 2026-09-13. What stays: the
state file in TerraformState (nothing prunes it until D28 is designed) and the
deploy repo's GitHub webhook. **Unregister** is deleting the entry directory.

## Previewing a migrating app's diff before its cutover

Before a migration registers its entry, a hand-made Application can show what
the first sync would change on the live Helm release. It renders the deploy repo
exactly as the generated Application will, but has no `syncPolicy` and no
resources finalizer. It is read, then deleted. Phase A.5 left this check open
([`phases.md`](../../../AnsibleSpecs/argo-cd/phases.md) A.5); KubeCoder's dev
stage is the first to run it.

What the manifest cannot show:

- **Never named `<app>-<stage>`.** The registry entry generates that name
  (above), and the ApplicationSet would take over an Application already
  holding it. Use `<app>-<stage>-preview`.
- **Never synced.** A sync runs the PreSync hook against
  `argocd/<repo>/<stage>/terraform.tfstate`, which holds nothing until the
  cutover's state surgery. That apply would try to create the live dataset, PV and
  webhook, and the sync would then apply the chart over the Helm-owned release.
  *Refresh* and *App Diff* are reads; *Sync* is not.
- **Deleted with `kubectl`, never the UI's *Delete*.** With no finalizer,
  `kubectl delete` removes the Application alone. The UI dialog defaults to a
  cascading policy, and argocd-server then adds the resources finalizer, which
  deletes the live namespace and everything in it.
- **Applying and deleting it are the operator's keystrokes**, both with the
  prd-write kubeconfig (Conventions).
- The notification subscription covers every Application, this one included.
  It never syncs, so only `ArgoCDHealthDegraded` can fire, and that reports the
  live release's health.

For KubeCoder's dev stage:

1. KubeCoderDeploy is pushed: Argo clones `origin/main`, never `/work`. List what
   HelmCharts changed in the chart since the copy, with the replay command in
   KubeCoderDeploy's `README.md`.
2. Apply. The quoted heredoc keeps `$ARGOCD_APP_REVISION` literal for Argo:

   ```sh
   cexec iac kubectl $KC apply -f - <<'EOF'
   apiVersion: argoproj.io/v1alpha1
   kind: Application
   metadata:
     name: kubecoder-dev-preview
     namespace: argocd-prd
   spec:
     project: releases
     source:
       repoURL: https://github.com/pvginkel/KubeCoderDeploy.git
       targetRevision: main
       path: chart
       helm:
         valueFiles:
           - ../config/dev/values.yaml
         # All four, as releases-local passes them: the library chart
         # required-guards each, so a missing one fails the whole render.
         parameters:
           - name: hook.repo
             value: https://github.com/pvginkel/KubeCoderDeploy.git
           - name: hook.revision
             value: $ARGOCD_APP_REVISION
           - name: hook.stage
             value: dev
           - name: hook.namespace
             value: kubecoder-dev
     destination:
       name: in-cluster
       namespace: kubecoder-dev
   EOF
   ```

3. Argo compares it on creation. The repo has no webhook until its cutover, so
   after a later push refresh it by hand (Webhooks).
4. Read the diff: the UI's *App Diff* with *Compact diff*, or each resource's
   *Diff* tab. The objects that differ:

   ```sh
   cexec iac kubectl $KC get application -n argocd-prd kubecoder-dev-preview \
     -o jsonpath='{range .status.resources[?(@.status=="OutOfSync")]}{.kind}/{.name}{"\n"}{end}'
   ```

5. Delete it, then confirm `kubecoder-dev` is still `Active`:

   ```sh
   cexec iac kubectl $KC delete application -n argocd-prd kubecoder-dev-preview
   cexec iac kubectl $KC get namespace kubecoder-dev
   ```

A sensible diff has these objects and these fields:

| Object | Expected difference |
| --- | --- |
| every object the chart renders | gains `argocd.argoproj.io/tracking-id` |
| `Namespace/kubecoder-dev` | gains `argocd.argoproj.io/sync-wave: "-1"` and `argocd.argoproj.io/sync-options: Prune=false` |
| `ConfigMap/kubecoder-controller-config` | the `worker` and `vsix` images: `dev-latest` → the pinned build |
| `Deployment/kubecoder-controller` | `deployment` (a timestamp) and `checksum/config` → the new controllerConfig checksum; the controller, ingress and manual images: a digest → the pin; `tunnel-reclaim`'s: a digest → `:latest` |
| `Deployment/kubecoder-bot`, `Deployment/kubecoder-mcp` | image: a digest → the pin |

The PreSync Job is a hook, so the diff never lists it. Anything else is the
finding: another object, another field, or an object *Missing*. The exception is
a difference that one of the HelmCharts commits from step 1 explains. That is
the chart re-sync slice 012 owes before its own review, not a defect.

## Bootstrapping Argo from nothing

As run on 2026-09-04. Only when the cluster, or the `argocd-prd` namespace, is
gone.

Before anything: `ArgoCDDeploy` pushed — the first self-sync clones
`origin/main`, so any bootstrap-time fix left unpushed is reverted by it; the
three leaves under `eso/prd/argocd/prd/` and the hook's under
`eso/prd/argocd-hooks/` written; the Keycloak client `argocd` present
(confidential, redirect URIs `https://argocd.home/auth/callback` and
`http://localhost:8085/auth/callback`); and the age keypair checked once —
`bao kv get -field=age_secret_key kv/iac/tf-backend | age-keygen -y` must print
the recipient committed in `config/prd/values.yaml`.

1. Give Helm a namespace it will adopt. The chart carries `Namespace/argocd-prd`
   as a tracked manifest, and `--create-namespace` makes Helm refuse it:

   ```sh
   cexec iac kubectl $KC create namespace argocd-prd
   cexec iac kubectl $KC label namespace argocd-prd app.kubernetes.io/managed-by=Helm
   cexec iac kubectl $KC annotate namespace argocd-prd \
     meta.helm.sh/release-name=argocd-prd meta.helm.sh/release-namespace=argocd-prd
   ```

2. Pre-apply the three CRDs server-side and stamp them the same way. The
   upstream subchart ships them as templates, not in `crds/`, so Helm cannot
   otherwise resolve the chart's own AppProject and ApplicationSet kinds; and
   `--server-side` is required because the rendered CRDs exceed the annotation
   limit a client-side apply writes into:

   ```sh
   cexec iac sh -c 'cd /work/ArgoCDDeploy && helm dependency build chart && \
     helm template argocd-prd chart -n argocd-prd -f config/prd/values.yaml \
       -s charts/argo-cd/templates/crds/crd-application.yaml \
       -s charts/argo-cd/templates/crds/crd-applicationset.yaml \
       -s charts/argo-cd/templates/crds/crd-appproject.yaml \
     | kubectl '"$KC"' apply --server-side -f -'
   for c in applications.argoproj.io applicationsets.argoproj.io appprojects.argoproj.io; do
     cexec iac kubectl $KC label crd $c app.kubernetes.io/managed-by=Helm --overwrite
     cexec iac kubectl $KC annotate crd $c meta.helm.sh/release-name=argocd-prd \
       meta.helm.sh/release-namespace=argocd-prd --overwrite
   done
   ```

3. Install. The release name must be `argocd-prd`, the Application's own name,
   or the first self-sync creates a renamed copy of every object beside the
   running ones:

   ```sh
   cexec iac sh -c 'cd /work/ArgoCDDeploy && \
     helm install argocd-prd chart --namespace argocd-prd --values config/prd/values.yaml'
   ```

4. Restart the applicationset-controller (above). Two startup races hit it on
   2026-09-04: `failed to create webhook handler` (it reached its one-shot
   settings read before argocd-server had generated `server.secretkey`), and a
   single generation attempt against a repo-server not yet listening, which is
   never retried. Both look like a healthy install with zero Applications.
5. Argo's registry entry already exists (HelmCharts
   `configs/prd/argocd/prd/release.yaml`); after the restart the `argocd-prd`
   Application appears OutOfSync. Sync it once by hand — Argo has adopted itself.
   Log in via SSO to confirm the client.
6. The registry webhook on HelmCharts exists and survives a rebuild; GitHub's
   creation ping, or a redelivery, should log "both receivers accepted" at the
   relay.

## Known behaviours

- **Namespace before hook.** The sync engine creates the destination Namespace
  ahead of the PreSync phase. App Terraform that creates it fails.
- **Sync-phase failures are not atomic.** Valid objects in the same wave are
  applied; only a hook failure leaves the cluster untouched.
- **Alerts live five minutes.** `on-sync-failed` fires once per condition and
  the template sets no end time, so Alertmanager expires the alert while the app
  is still failed, and Telegram gets a `[RESOLVED]` notice for it.
- **Hook Jobs accumulate** for an app's lifetime; the delete policy never
  matches a name carrying SHA and timestamp. They go with the Application.
- **Rebuilds.** Argo runs on prd only and keeps no node-local state, so a prd
  node rebuild does not touch it. `k8s-rebuild.md`'s note that HelmCharts
  releases on `srvk8sdev` need redeploying after a dev rebuild still holds.
