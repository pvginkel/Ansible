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

An app that HelmCharts deploys today moves onto Argo when this entry replaces its
`jenkins` one. A stage with no `release.yaml`, or with no `reconciler:` key, is
`jenkins`'s. The same push drops the stage from HelmCharts' architecture
artifact, so the app's own architecture producer is registered before it
([Giving an app its own architecture producer](#giving-an-app-its-own-architecture-producer)).

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

## Giving an app its own architecture producer

An app appears in the federated architecture model only while a registered
producer publishes it. HelmCharts' `helm-charts` producer publishes each stage
that is `jenkins`'s from the annotation file `charts/<app>/architecture.yaml`. It
leaves a stage out once that stage's entry says `reconciler: argo-cd`. So a
migrating app carries a producer of its own in its deploy repo, registered
before that flip, or it drops out of the model. An app new to the estate carries
one too. It has no current producer, so it has no flip to order against.

The generator is `gen-architecture`, from ArgoCDTools' `aac-tools` image. It
renders `chart/` with `config/<stage>/values.yaml` the way Argo renders it, reads
the judgment layer, and writes `docs/architecture/<producer>.yaml`. Its ids are
uuid5s of the same natural keys, under the same namespace constant, that
HelmCharts' generator uses. So the new producer mints the ids `helm-charts`
publishes today, and no other producer's edge into the app dangles across the
handover.

The worked examples, to copy from:

| Deploy repo | Producer | Publishes | Case |
| --- | --- | --- | --- |
| `ArgoCDDeploy` | `argocd-deploy` | prd, from `main` | a new app: no current producer, no flip |
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

What a migration would otherwise get wrong:

- **One pipeline publishes one stage, from one branch.** The artifact is
  attached to the pipeline, so a pipeline that built two stages would publish
  them alternately. `Jenkinsfile.architecture` names the branch Argo syncs the
  stage from and passes that stage to `--stage`, which is required and takes one
  value. That is the whole guard. The generator does not read the branch, and it
  has no rule about which stages an app publishes. KubeCoder publishes prd only.
- **The moved annotation file states `introduced:`.** HelmCharts derives the
  date from the first commit that adds `charts/<app>`. A deploy repo's history
  dates the repo, not the app, so the generator requires the key and has no
  fallback. Copy the file verbatim and add HelmCharts' date. Every published
  element of the app carries that date, so a different date keeps the ids but
  changes every element:

  ```sh
  git -C /work/HelmCharts log --diff-filter=A --reverse --format=%ad --date=short -- charts/<app> | head -1
  ```

  HelmCharts keeps publishing the app from its own copy until the flip, so the
  two copies coexist until then. Record the copied commit in the deploy repo's
  `README.md`, and replay any change to HelmCharts' copy. A new app takes the
  date of the first commit that adds its deploy repo's `chart/`, as
  ArgoCDDeploy's does.
- **The producer id is `<app>-deploy`**, where `<app>` is `name:` in
  `chart/Chart.yaml`. The generator keys every id on the chart's name, while
  Argo names the Application after the registry directory `configs/prd/<app>/`.
  The two must be equal, and nothing checks that yet. The id is not the repo name
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

The order runs from a committed producer to a registered one. Steps 4 and 5 are
the operator's:

1. Commit the files. `kc project test` in the deploy repo generates and
   validates the artifact. Two regenerations from the same commit must be
   byte-identical. A difference means render-time randomness reaches an id or
   an emitted field.
2. At a handover, prove that the new producer's artifact equals what
   `helm-charts` publishes for the stage. The check clones the checkout's HEAD,
   so commit the judgment layer first:

   ```sh
   cd /work/ArgoCDTools
   cexec iac python3 aac-tools/checks/handover_equality.py \
     --deploy-repo /work/<Repo> --stage <stage> --producer <app>-deploy
   ```

   Exit 0 means the same element and relation ids, with every field equal except
   those the check sets aside. ArgoCDTools' README (Gates) says how to read a
   difference.
3. Push the published branch. A promotion branch must exist and carry the
   producer. KubeCoderDeploy's `prd` is created from `main` at its prd cutover.
4. Create the Jenkins job `AaC/<Repo>` to run `Jenkinsfile.architecture`, and
   build it. The first green build archives `docs/architecture/<app>-deploy.yaml`.
5. Only after that green build, register the producer with a PR against
   `pipeline-producers.yaml` in `pvginkel/Architecture`:

   ```yaml
   - id: <app>-deploy
     repo: pvginkel/<Repo>
     jenkinsJob: AaC/<Repo>
   ```

   A registered producer with no archived artifact fails the collector's
   discovery, and a failed collector run publishes nothing. `repo:` enrols the
   producer in the central architecture update.
6. At a handover, flip the stage: push its registry entry
   ([above](#registering-undeploying-and-unregistering-an-app)). From
   registration until the flip, both producers declare the app's ids. The
   collector fails on the duplicates, the Architecture job is red, and the
   published model keeps the app as it was. The flip's HelmCharts architecture
   build leaves the stage out, and the next collector run is green, with the new
   producer owning the same ids. Flipping first would publish a green model
   without the app. Each stage flips on its own: a stage that the new producer
   does not publish leaves the model at its flip and needs no registration
   first. A new app has no step 6.

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
  (above), and the ApplicationSet would take over an Application already
  holding it. Use `<app>-<stage>-preview`.
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
         # The generated Application is named kubecoder-dev and renders that as
         # the release name; the preview has to say so itself.
         releaseName: kubecoder-dev
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
| `Deployment/kubecoder-controller` | `deployment` (a timestamp) and `checksum/config` → the new controllerConfig checksum; the controller, ingress and manual containers: image a digest → the pin, `imagePullPolicy` `Always` → `IfNotPresent`; `tunnel-reclaim`'s image: a digest → `:latest` |
| `Deployment/kubecoder-bot`, `Deployment/kubecoder-mcp` | image: a digest → the pin; `imagePullPolicy`: `Always` → `IfNotPresent` |

The PreSync Job is a hook, so the diff never lists it. Anything else is the
finding: another object, another field, or an object *Missing*. The exception is
a difference that one of the HelmCharts commits from step 1 explains. That is a
replay KubeCoderDeploy still owes, not a defect.

prd's set has the same rows, with `kubecoder-prd` in place of `kubecoder-dev` and
`prd-latest` in place of `dev-latest`. It has one more object:
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
  -n <ns> -o json --show-managed-fields > /tmp/live.json
# cluster-scoped objects one at a time, passed as extra arguments
python3 /work/AnsibleSpecs/handovers/argo-adoption-blind-spot/stuck_fields.py \
  <ns> /tmp/render.yaml /tmp/live.json /tmp/ns.json /tmp/clusterrole.json
```

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

Working notes, the probes behind the mechanism, and the full per-stage
inventory, taken before the chart declared the pull policy:
[`handovers/argo-adoption-blind-spot/`](../../../AnsibleSpecs/handovers/argo-adoption-blind-spot/findings-2026-09-20.md).

The alternative to adopting in place is recreating: delete the namespace and let
Argo build the release from nothing, which needs no enumeration because nothing
is inherited. It costs an outage of everything in the stage and leaves the PV
`Released` with a stale `claimRef` to clear, so it suits a stage with no live
state to interrupt. Which of the two is the estate's default is not yet decided.

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
