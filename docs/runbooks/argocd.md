# Argo CD operator runbook

Day-to-day operation of the Argo CD instance on the prd cluster: reading its
state without the CLI, diagnosing a failed sync, webhooks, rotating its tokens,
upgrading it, getting in when SSO is broken, registering and removing an app,
giving an app its own architecture producer, and rebuilding Argo from nothing.
Read this when a sync fails, a token or secret changes, or Argo itself needs an
upgrade or a bootstrap.

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
  below is `kubectl` with the prd-write kubeconfig. The read-only default
  kubeconfig can list Applications, ApplicationSets and AppProjects, but not
  patch or annotate them. Shorthand used throughout:

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
| UI | `https://argocd.home`, or the bare `https://argocd` — Keycloak SSO (realm `homelab`, client `argocd`) |
| Deploy repo | `ArgoCDDeploy`: exact `argo-cd` pin in `chart/Chart.yaml`, stage values in `config/prd/values.yaml` |
| Registry | ArgoCDDeploy `releases/values.yaml`: one entry per app, one Application per stage (D63); `releases/values.schema.json` refuses a malformed entry |
| Registry Application | `releases`: syncs the registry chart `releases/` from ArgoCDDeploy `main`, automated without prune or self-heal |
| Webhook edge | `https://deploy-hooks.webathome.org/api/webhook` → relay (2 replicas) → argocd-server and the applicationset-controller |
| Hook image | `registry:5000/argocd-hook:<n>` from ArgoCDTools; default pin in the `homelab-shared` library chart. Its Terraform is pinned to the version the `iac` images carry (AnsibleSpecs `decisions.md`, "Terraform version") |
| Terraform state | `pvginkel/TerraformState`, `argocd/<repo>/<stage>/terraform.tfstate`, sops/age |
| Notifications | Alertmanager `prometheus-prd-alertmanager.prometheus-prd:9093`, delivered to Telegram with no "resolved"; `ArgoCDSyncFailed` (critical, with sound), `ArgoCDHealthDegraded` (warning, silent) |
| Standing alerts | PrometheusDeploy's rule group `argocd`, over the application controller's metrics (Service `argocd-prd-application-controller-metrics`); `ArgoCDSyncStillFailed` (critical), `ArgoCDHealthStillDegraded` and `ArgoCDAlertsBlind` (warning) |

> The two registry rows are owed until the registry switch has run ([registry-switch.md](registry-switch.md)): until then the ApplicationSets `releases-local` and `releases-upstream` generate the Applications from HelmCharts' `configs/prd/<app>/<stage>/release.yaml` files.

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
# The app's conditions: a SyncError says why Argo stopped auto-syncing it
cexec iac kubectl $KC get application -n argocd-prd <app> \
  -o jsonpath='{range .status.conditions[*]}{.type}: {.message}{"\n"}{end}'
# Per-resource results of that operation (hooks included)
cexec iac kubectl $KC get application -n argocd-prd <app> \
  -o jsonpath='{range .status.operationState.syncResult.resources[*]}{.kind}/{.name} {.hookPhase}{.status}: {.message}{"\n"}{end}'
# The hook Job and its logs: one per app, tf-presync-<app>-<stage>, holding its latest run
cexec iac kubectl $KC get jobs,pods -n argocd-hooks
cexec iac kubectl $KC logs -n argocd-hooks job/tf-presync-<app>-<stage>
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
   webhook (below). A registry entry reaches its Application only through
   `releases`' sync, so read `releases`' last operation and conditions too.

   > This item's `releases` half is owed until the registry switch has run ([registry-switch.md](registry-switch.md)): until then suspect the applicationset-controller's one-shot handler (below).

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
GitHub token can list hooks (`cexec iac gh api repos/pvginkel/<repo>/hooks`) but not
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

It serves only the ApplicationSets `releases-local` and `releases-upstream`, which the registry
switch deletes ([registry-switch.md](registry-switch.md), step 5).

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
2. ArgoCDDeploy's relay webhook delivers the push, and `argocd-prd` goes
   OutOfSync within seconds. Review the diff in the UI. The CRDs sync
   server-side (`ServerSideApply=true` on all three).

   > Owed until the registry switch has run ([registry-switch.md](registry-switch.md)), whose step 2 adds that webhook: until then refresh `argocd-prd` by hand (Webhooks).

3. Sync by hand at a chosen moment (D3). The controller and repo-server restart
   mid-sync, and every Application pauses with them.
4. Verify: `argocd-prd` Synced and Healthy, every pod Running, the full
   Application list back, and a webhook delivery accepted by both receivers. If
   the list does not come back, see item 4 of Diagnosing a failed sync.

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

> This section is owed until the registry switch has run ([registry-switch.md](registry-switch.md)): until then an edit to ArgoCDDeploy's registry deploys nothing.

The registry is ArgoCDDeploy's `releases/values.yaml` (D63). An app's entry sits
under `apps:`, and each of its stages becomes one Application, named and
namespaced `<app>-<stage>`:

```yaml
apps:
  <app>:
    repo: https://github.com/pvginkel/<DeployRepo>.git
    stages:
      prd:
        autoSync: false
```

An app whose chart comes from a Helm repository adds `upstream: {repo, chart}`,
and each of its stages pins the chart's `version`. A stage may set
`targetRevision` (default `main`), and `syncOptions` is app-level: every stage's
Application gets it (D62). The file's header comment names every key, and
`releases/values.schema.json` refuses anything else. Keep entries alphabetical.
`helm lint releases` checks an edit against the schema, and ArgoCDDeploy's
`kc project test` runs the render test. Push; the relay webhook refreshes
`releases`, whose sync creates the Application OutOfSync. The first sync is
manual.

What a sync does, in order: Argo applies the chart's `sync-wave: "-1"` Namespace
during its dry-run pass; the PreSync Job runs in `argocd-hooks` (clone at the
synced SHA → terraform-backend-git → `terraform apply` with the stage tfvars →
PV reattach); then the Sync waves. The app's Terraform takes `var.namespace` as
given and never creates it.

A stage without `autoSync`, or with `autoSync: true`, gets `syncPolicy.automated`
(`prune: true`, `selfHeal: false`) and D5's retry block, and Argo syncs it on its
own; `autoSync: false` renders no policy. Either change reaches the Application
with `releases`' sync of the push.

**Undeploy** a stage by deleting its entry. `releases` does not prune, so the
Application stays, shown as requiring pruning, until the operator syncs
`releases` with *Prune* ticked (D27 as amended). Then the resources finalizer
cascades: workloads, the `Prune=false` Namespace and the hook Jobs were all gone
within 45 seconds on 2026-09-13. What stays: the state file in TerraformState
(nothing prunes it until D28 is designed) and the deploy repo's GitHub webhook.
**Unregister** is deleting the app's whole entry, which undeploys each stage the
same way.

## Giving an app its own architecture producer

An app appears in the federated architecture model only while a registered
producer publishes it, so every app on Argo carries a producer of its own in its
deploy repo. During the migration, HelmCharts' `helm-charts` producer published
each stage HelmCharts deployed, and each migrating app's producer took its stage
over at a handover. That producer is retired from Architecture's
`pipeline-producers.yaml` (slice 029), so an app that joins the model now has no
current producer and nothing to hand over from.

The generator is `gen-architecture`, from ArgoCDTools' `aac-tools` image. It
renders `chart/` with `config/<stage>/values.yaml` the way Argo renders it, reads
the judgment layer, and writes `docs/architecture/<producer>.yaml`. Its ids are
uuid5s of the same natural keys, under the same namespace constant, that
HelmCharts' generator used, so each migrated app kept its ids across its
handover.

A provider in another app resolves through the published set. The generator
publishes each Service it places on its workloads as an interface at its
in-cluster host, `<svc>.<ns>.svc`. It links every interface, exposed hosts
included, to the instances serving behind it (D55). A host the render cannot
place resolves to the instances that the published interfaces at that host
link. A host that resolves nowhere fails the build, and no artifact is written.
So a consumer builds only once its provider is published. Nothing re-runs the
consumer when the provider first publishes, so a new app whose provider is not
yet published is bootstrapped by hand.

The worked examples, to copy from:

| Deploy repo | Producer | Publishes | Case |
| --- | --- | --- | --- |
| `ArgoCDDeploy` | `argocd-deploy` | prd, from `main` | a new app: no current producer |
| `KubeCoderDeploy` | `kubecoder-deploy` | prd, from `prd` | a handover from `helm-charts`; dev is not published |

What the deploy repo carries:

- **`architecture.yaml`** at the root: the judgment layer. Its schema is the
  docstring of ArgoCDTools' `aac-tools/image/gen_architecture.py`.
- **`Jenkinsfile.architecture`**: it clones the published branch. Then, in the
  `aac-tools` container (`containerTemplates.aac_tools('aac-tools')`), it runs
  `gen-architecture --stage <stage> --producer <app>-deploy` and `arch-validate
  docs/architecture/*.yaml`, and it archives `docs/architecture/*.yaml`. The
  collector copies only artifacts whose path contains an `architecture/`
  directory.
- **`.architecturerc`**: see below.
- **`/docs/architecture/` in `.gitignore`**: the artifact is build output and is
  never committed.
- **`.kubecoder/project.yaml`**: `jenkins: AaC/<Repo>`. The last two `test`
  statements are the pipeline's two commands: `cexec aac-tools gen-architecture
  --stage <stage> --producer <app>-deploy` and `cexec aac-tools arch-validate
  docs/architecture/<app>-deploy.yaml`.

What a new producer would otherwise get wrong:

- **One pipeline publishes one stage, from one branch.** The artifact is
  attached to the pipeline, so a pipeline that built two stages would publish
  them alternately. `Jenkinsfile.architecture` names the branch Argo syncs the
  stage from and passes that stage to `--stage`, which is required and takes one
  value. That is the whole guard. The generator does not read the branch, and it
  has no rule about which stages an app publishes. KubeCoder publishes prd only.
- **The annotation file states `introduced:`.** A deploy repo's history dates
  the repo, not the app, so the generator requires the key and has no fallback.
  A new app takes the date of the first commit that adds its deploy repo's
  `chart/`, as ArgoCDDeploy's does. An annotation file copied from HelmCharts'
  `charts/<app>/architecture.yaml` keeps HelmCharts' date, the first commit that
  adds `charts/<app>`. Every published element of the app carries that date:

  ```sh
  git -C /work/HelmCharts log --diff-filter=A --reverse --format=%ad --date=short -- charts/<app> | head -1
  ```

- **The producer id is `<app>-deploy`**, where `<app>` is `name:` in
  `chart/Chart.yaml`. The generator keys every id on the chart's name, while
  Argo names the Application after the app's registry entry. The two must be
  equal, and nothing checks that yet. The id is not the repo name
  in kebab case, which would give `kube-coder-deploy`.
- **`.architecturerc` names the real sources.** The default `sources`,
  `:(glob)**/docs/architecture/**`, matches nothing at a deploy repo's head,
  because the artifact is not committed. The central architecture update fails a
  producer whose `sources` match nothing. The file carries these three keys and
  no others, because any other key also fails the producer:

  ```yaml
  generated: true
  sources:
    - architecture.yaml
    - chart/
    - config/<stage>/
  instructions: >
    The artifact is build output; edit only the judgment layer, architecture.yaml.
  ```

- **An owned product is minted once.** A `products:` entry makes this producer
  publish that «SoftwareProduct» element, and the element's id is a uuid5 of its
  natural key. A second producer that declares the same product mints the same
  id, and the collector then fails on the duplicate and publishes nothing. Search
  the published dataset first. Map an image to a product that another producer
  already publishes rather than owning it again. `argocd-deploy` owns
  `ss:argo-cd` and `ss:redis`.
- **Gaps are printed, not fatal.** When the render carries an image that
  `images:` does not map, the generator prints `gap: <what>` and the build stays
  green. The central architecture update reads those lines from the last green
  build. Map the image, or know why it stays a gap.

The order runs from a committed producer to a registered one. Steps 3 and 4 are
the operator's:

1. Commit the files. `kc project test` in the deploy repo generates and
   validates the artifact. Two regenerations from the same commit must be
   byte-identical. A difference means render-time randomness reaches an id or
   an emitted field.
2. Push the published branch. A promotion branch must exist and carry the
   producer. KubeCoderDeploy's `prd` is created from `main` by its promote job's
   first run, at the prd cutover.
3. Create the Jenkins job `AaC/<Repo>` to run `Jenkinsfile.architecture`, and
   build it. The first green build archives `docs/architecture/<app>-deploy.yaml`.
4. Only after that green build, register the producer with a PR against
   `pipeline-producers.yaml` in `pvginkel/Architecture`:

   ```yaml
   - id: <app>-deploy
     repo: pvginkel/<Repo>
     jenkinsJob: AaC/<Repo>
   ```

   A registered producer with no archived artifact fails the collector's
   discovery, and a failed collector run publishes nothing. `repo:` enrols the
   producer in the central architecture update.

Registering the producer and adding the app's registry entry
([above](#registering-undeploying-and-unregistering-an-app)) do not wait on each
other.

The central architecture update (`tooling/fleet.py` in `pvginkel/Architecture`)
does not yet serve a producer correctly when that producer builds a promotion
branch rather than the default branch. The update clones the `repo:` at its
default branch and pushes its edits there. `kubecoder-deploy` builds `prd`, and
its default branch is `main`. So what the update edits is not what the pipeline
publishes until `prd` is promoted.

## Previewing a migrating app's diff before its cutover

Before a migration registers its entry, a hand-made Application can show what
the first sync would change on the live Helm release. It renders the deploy repo
exactly as the generated Application will, but has no `syncPolicy` and no
resources finalizer. It is read, then deleted. Phase A.5 left this check open
([`phases.md`](../../../AnsibleSpecs/argo-cd/phases.md) A.5); KubeCoder's dev
stage is the first to run it.

What the manifest cannot show:

- **Never named `<app>-<stage>`.** The registry entry generates that name
  (above), and the registry would take over an Application already holding it.
  Use `<app>-<stage>-preview`.
- **`helm.releaseName` pinned back to `<app>-<stage>`.** Argo passes the
  Application's name to `helm template` as the release name unless
  `spec.source.helm.releaseName` overrides it, so a preview would render
  `.Release.Name` as `<app>-<stage>-preview` where the generated Application
  renders `<app>-<stage>`. A chart that reads `.Release.Name` would then show
  differences its real first sync never makes. KubeCoder's chart reads only
  `.Release.Namespace` and is unaffected; set it regardless, so the next
  migration's preview is faithful without anyone having to notice.
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
  It never syncs, so only `ArgoCDHealthDegraded` and, fifteen minutes on,
  `ArgoCDHealthStillDegraded` can fire, and they report the live release's
  health.

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
         # The generated Application is named kubecoder-dev and renders that as
         # the release name; the preview has to say so itself.
         releaseName: kubecoder-dev
         valueFiles:
           - ../config/dev/values.yaml
         # All four, as the registry passes them: the library chart
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
| `ConfigMap/kubecoder-controller-config` | the `worker` and `vsix` images: `dev-latest` → the pinned build's `dev-<n>` |
| `Deployment/kubecoder-controller` | `deployment` (a timestamp) and `checksum/config` → the new controllerConfig checksum; the controller, ingress and manual containers: image a digest → the pin, `imagePullPolicy` `Always` → `IfNotPresent`; `tunnel-reclaim`'s image: a digest → `:latest` |
| `Deployment/kubecoder-bot`, `Deployment/kubecoder-mcp` | image: a digest → the pin; `imagePullPolicy`: `Always` → `IfNotPresent` |

The PreSync Job is a hook, so the diff never lists it. Anything else is the
finding: another object, another field, or an object *Missing*. The exception is
a difference that one of the HelmCharts commits from step 1 explains. That is a
replay KubeCoderDeploy still owes, not a defect.

prd's set has the same rows, with `kubecoder-prd` in place of `kubecoder-dev`, and
`prd-latest` and `prd-<n>` in place of `dev-latest` and `dev-<n>`. It has one more object:
`Service/kubecoder-mcp-public`, which only prd's render carries, and which gains
the tracking-id and nothing else. The cutover itself reviews the generated
Application, not a preview:
[`kubecoder-cutover.md`](kubecoder-cutover.md).

## What a cutover does not change

The preview above shows what the first sync *will* do. It cannot show what the
sync leaves alone, and that set is not empty: **a field the old Helm chart set
and the new render omits survives the cutover, at its Helm value, indefinitely.**

Argo's default apply is client-side. To tell "a field I used to declare and have
now dropped" from "a field the API server defaulted", it reads
`kubectl.kubernetes.io/last-applied-configuration` — which Helm-created objects
do not carry, because Helm applies server-side and writes none. Without it the
three-way merge degrades to a two-way one whose delete bucket is always empty.
Turning on server-side apply does not rescue it either: the field is owned by
the field manager `helm`, and server-side apply deletes a field only when the
manager that owns it stops declaring it.

It is a one-time artifact. Argo's first apply writes the annotation itself, so
every later chart change removes fields normally. Only what the *old* chart set
and the new one never mentions stays frozen.

**The pre-flight.** A field sticks exactly when `metadata.managedFields` says
`helm` owns it and the render never declares it; an object Helm created that the
render does not contain is never adopted and never pruned. Both are computable
before the cutover, and should be, for every migrating app:

```sh
cexec iac helm template <ns> chart --namespace <ns> \
  --values config/<stage>/values.yaml \
  --set hook.repo=<repo>,hook.revision=<sha>,hook.stage=<stage>,hook.namespace=<ns> > /tmp/render.yaml
cexec iac kubectl $KC get serviceaccount,configmap,secret,persistentvolumeclaim,service,\
deployment,statefulset,daemonset,job,cronjob,ingress,networkpolicy,role,rolebinding,externalsecrets \
  -n <ns> -o json --show-managed-fields \
  | jq '.items |= map(if .kind == "Secret" then del(.data, .stringData,
      .metadata.annotations["kubectl.kubernetes.io/last-applied-configuration"]) else . end)' \
  > /tmp/live.json
# cluster-scoped objects one at a time, passed as extra arguments
python3 /work/Ansible/support/argo-migrate/stuck_fields.py \
  <ns> /tmp/render.yaml /tmp/live.json /tmp/ns.json /tmp/clusterrole.json
```

The `jq` filter drops every Secret's values before the dump reaches disk: the
test reads only `managedFields`, which names a Secret's keys but never holds its
values, so nothing the output depends on is lost and no credential lands in
`/tmp`.

Read the two lists it prints. An object absent from the render is either
something the deploy repo forgot, or output of another controller — ESO's
materialised Secrets show up here and are not findings, since the render carries
the `ExternalSecret` that produces them. A stuck field is a decision: declare it
in the chart (which takes ownership, and is what makes it changeable afterwards),
patch it out once at cutover, or accept it.

For KubeCoder the residue is two things. The first is a stale `deployment`
annotation on the bot and MCP pod templates. The second, on every adopted object,
is `metadata.labels` and `metadata.annotations`, which the chart does not render
at all. So `app.kubernetes.io/managed-by: Helm` and `meta.helm.sh/release-name`
outlive the migration. That last one is inert in itself, but anything keyed on
those labels keeps reading a migrated app as Helm-managed.

The five pinned containers' `imagePullPolicy`, which Helm set to `Always`, is not
residue. KubeCoderDeploy's chart declares it `IfNotPresent`, so the first sync
takes the field over, and the diff table above shows the change.

The mechanism was proven on 2026-09-20 on a throwaway ConfigMap: the field
manager `helm` created it with two fields, and `argocd-controller` then applied
it with one. The other field survived a client-side apply, a server-side apply
with `--force-conflicts`, and a server-side re-apply after helm's
`managedFields` entry had been deleted. It went only once `argocd-controller`
had declared the field itself and a later apply dropped it. The pre-flight's
first run that day, before KubeCoderDeploy's chart declared the pull policy,
counted 52 stuck fields on `kubecoder-dev` and 55 on `kubecoder-prd`, every one
of them the residue above or that pull policy.

The alternative to adopting in place is recreating: delete the namespace and let
Argo build the release from nothing, which needs no enumeration because nothing
is inherited. It costs an outage of everything in the stage and leaves the PV
`Released` with a stale `claimRef` to clear, so it suits a stage with no live
state to interrupt. Which of the two is the estate's default is not yet decided.

## Bootstrapping Argo from nothing

Steps 1 to 3 are as run on 2026-09-04. Only when the cluster, or the
`argocd-prd` namespace, is gone.

> Steps 4 to 6 are owed until the registry switch has run ([registry-switch.md](registry-switch.md)): until then restart the applicationset-controller (above) in place of step 4, which then generates `argocd-prd` from HelmCharts' `configs/prd/argocd/prd/release.yaml`, and the registry webhook of step 6 is HelmCharts'.

Before anything: `ArgoCDDeploy` pushed — the first self-sync clones
`origin/main`, so any bootstrap-time fix left unpushed is reverted by it; the
three leaves under `eso/prd/argocd/prd/` and the hook's under
`eso/prd/argocd-hooks/` written; the Keycloak client `argocd` present
(confidential, redirect URIs `https://argocd.home/auth/callback`,
`https://argocd/auth/callback` and `http://localhost:8085/auth/callback`); and
the age keypair checked once —
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

4. The install brings up `releases`, which syncs on its own and creates every
   Application in the registry. Read it (Reading Argo without the CLI): Synced,
   and the Application list back. If `releases` shows a `ComparisonError` or has
   created nothing, refresh it by hand (Webhooks).
5. Argo's registry entry already exists (ArgoCDDeploy `releases/values.yaml`,
   `apps.argocd`), so the `argocd-prd` Application appears OutOfSync. Sync it
   once by hand — Argo has adopted itself. Log in via SSO to confirm the client.
6. ArgoCDDeploy's relay webhook exists and survives a rebuild; GitHub's creation
   ping, or a redelivery, logs "both receivers accepted" at the relay.

## Known behaviours

- **Namespace before hook.** The sync engine creates the destination Namespace
  ahead of the PreSync phase. App Terraform that creates it fails.
- **Sync-phase failures are not atomic.** Valid objects in the same wave are
  applied; only a hook failure leaves the cluster untouched.
- **A failure alerts twice: the event, then the state.** `on-sync-failed`, with
  Argo's error text, and `on-health-degraded` fire once per condition. The
  templates set no end time, so Alertmanager expires the event after its
  five-minute resolve timeout, failed app or not, and its receivers send no
  "resolved". The state is PrometheusDeploy's: `ArgoCDSyncStillFailed` fires ten
  minutes after Argo sets `SyncError` on an app and stops auto-syncing it, as
  when its retries are spent on a revision it will not try again on its own, or
  its prune guard refuses a sync that would delete every resource;
  `ArgoCDHealthStillDegraded` fires after fifteen minutes Degraded. Each stays
  up until the app recovers, for a failed sync a new commit or a manual sync
  that succeeds, then resolves. An app Argo does not auto-sync, its own among
  them, gets no standing sync alert: its failed sync is the event alone.
  `ArgoCDAlertsBlind` fires when Prometheus has had no application metrics from
  the controller for fifteen minutes, which leaves both blind.
- **Hook Jobs accumulate** for an app's lifetime; the delete policy never
  matches a name carrying SHA and timestamp. They go with the Application.
- **Rebuilds.** Argo runs on prd only, deploys only into prd (`in-cluster`) and
  keeps no node-local state, so neither a prd node rebuild nor a `srvk8sdev`
  rebuild touches it.
