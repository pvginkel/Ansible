# Argo CD operator runbook

Day-to-day operation of the Argo CD instance on the prd cluster: reading its
state, diagnosing a failed sync, webhooks, rotating its tokens,
upgrading it, getting in when SSO is broken, registering and removing an app,
destroying what a retired stage left, giving an app its own architecture
producer, and rebuilding Argo from nothing.
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
- A prd KubeCoder environment reads Argo through the `argocd` CLI in its `iac`
  sidecar, as the read-only `kubecoder` account and with no login step
  ([Reading Argo with the CLI](#reading-argo-with-the-cli)). It writes nothing.
  Every write is the operator's keystroke, in the UI or through `kubectl` with
  the prd-write kubeconfig. The read-only default kubeconfig can list
  Applications, ApplicationSets and AppProjects, but not patch or annotate
  them. Shorthand used throughout:

  ```sh
  KC="--kubeconfig $HOME/.kube/config-prd-write --context prd"
  cexec iac kubectl $KC get applications.argoproj.io -A
  ```

- The webhook is the trigger (D6). A dropped delivery is picked up by the
  30-minute periodic refresh, which is also what brings an app's health up to
  date after a rollout: Argo CD 3.x ignores `/status`-only updates. An app
  that shows Progressing past its rollout catches up within half an hour, or
  at once on a manual Refresh.
- Argo's own Application never auto-syncs (D3). Every Argo upgrade is a manual
  sync at a moment the operator picks.

## Facts

| Item | Value |
| --- | --- |
| Namespaces | `argocd-prd` (Argo, the webhook relay), `argocd-hooks` (PreSync Jobs, Destroy Stage Jobs, the `tf-presync` ServiceAccount, `argocd-hook-credentials`) |
| Helm release, Application, AppProject | `argocd-prd`, `argocd-prd`, `releases` |
| UI | `https://argocd.home`, or the bare `https://argocd` — Keycloak SSO (realm `homelab`, client `argocd`) |
| Read-only account | `kubecoder`: API tokens only, no password, bound to `role:readonly`; its token is `ARGOCD_AUTH_TOKEN` in every prd KubeCoder environment |
| Deploy repo | `ArgoCDDeploy`: exact `argo-cd` pin in `chart/Chart.yaml`, stage values in `config/prd/values.yaml` |
| Registry | ArgoCDDeploy `releases/values.yaml`: one entry per app, one Application per stage (D63); `releases/values.schema.json` refuses a malformed entry |
| Registry Application | `releases`: syncs the registry chart `releases/` from ArgoCDDeploy `main`, automated without prune or self-heal |
| Webhook edge | `https://deploy-hooks.webathome.org/api/webhook` → relay (2 replicas) → argocd-server |
| Hook image | `registry:5000/argocd-hook:<n>` and `:latest` from ArgoCDTools; the sync's default pin is in the `homelab-shared` library chart, and the Destroy Stage Job runs `:latest`. Its Terraform is pinned to the version the `iac` images carry (AnsibleSpecs `decisions.md`, "Terraform version") |
| Terraform state | `pvginkel/TerraformState`, `argocd/<repo>/<stage>/terraform.tfstate`, sops/age |
| Destroy Stage | Jenkins job `IaC/Destroy Stage`, ArgoCDTools `Jenkinsfile.destroy-stage`; the build runs as `jenkins-prd/destroy-stage`, its Job is `argocd-hooks/destroy-stage-<build#>` under `tf-presync` (D66) |
| Notifications | Alertmanager `prometheus-prd-alertmanager.prometheus-prd:9093`, delivered to Telegram with no "resolved"; `ArgoCDSyncFailed` (critical, with sound), `ArgoCDHealthDegraded` (warning, silent) |
| Standing alerts | PrometheusDeploy's rule group `argocd`, over the application controller's metrics (Service `argocd-prd-application-controller-metrics`); `ArgoCDSyncStillFailed` (critical), `ArgoCDHealthStillDegraded` and `ArgoCDAlertsBlind` (warning) |

Every credential arrives through ESO from OpenBao (`kv/` mount), refreshed hourly:

| ExternalSecret | Leaf and property | Reader |
| --- | --- | --- |
| `argocd-prd/argocd-repo-creds-github` | `eso/prd/argocd/prd/git#token` | Argo's own repo clones (classic PAT, `repo`) |
| `argocd-prd/argocd-webhook` | `eso/prd/argocd/prd/webhook#github_secret` | argocd-server and the relay; the same value GitHub holds on every hook |
| `argocd-prd/argocd-oidc` | `eso/prd/argocd/prd/oidc#client_secret` | SSO |
| `argocd-hooks/argocd-hook-credentials` | `eso/prd/argocd-hooks/git#token` plus nine more leaves, 23 keys — `webhook#github_secret` above among them, as `TF_VAR_github_webhook_secret` | the PreSync hook: its clone, state pushes, provider credentials, the secret a deploy repo's webhook is signed with; the Destroy Stage Job, the same way |

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

## Reading Argo with the CLI

In a prd KubeCoder environment, `iac` carries the `argocd` CLI, and the
environment carries `ARGOCD_SERVER=argocd.home`, `ARGOCD_OPTS=--grpc-web` and
the `kubecoder` account's token as `ARGOCD_AUTH_TOKEN`. The CLI reads with no
login step. These two reads answer what the UI's *App Diff* tab shows:

```sh
# Live state against the target revision's render; exits 1 when they differ
cexec iac argocd app diff <app>
# The target revision's rendered manifests (--source live for the live ones)
cexec iac argocd app manifests <app>
```

The account is ArgoCDDeploy's `accounts.kubecoder: apiKey`, and
`g, kubecoder, role:readonly` in `policy.csv` binds it to Argo's built-in
read-only role, which grants `get` on every resource type and nothing else.
`cexec iac argocd account can-i sync applications '*/*'` answers `no`. Its
token: [The `kubecoder` account's token](#the-kubecoder-accounts-token). The
dev deployment's environments carry none of this.

## Diagnosing a failed sync

1. **Operation `Failed`, the only failed result a `Job/tf-presync-…` with "Job
   has reached the specified backoff limit".** The hook failed and nothing was
   applied. Read the Job's log (`tf-presync-<app>-<stage>`, the same name for
   every run; a retry replaces it, see below). Seen so far:
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

Hook Jobs persist one per app, holding only the latest attempt
(`backoffLimit: 0`, so a failed hook is exactly one pod), and are removed with
the Application. A retry (`retry.limit: 3`) or the next sync replaces the Job
under the same name, and the failed pod goes with it, 60 to 90 s after
`BackoffLimitExceeded`; events keep no log.

### A replaced hook's log: Kibana

Filebeat (`filebeat-prd`) ships every container log on prd to Elasticsearch,
kept 7 days, and each hook attempt is a pod of its own name, so a replaced
attempt's log stays there. In `https://kibana.home` → Discover, data view
`filebeat-*`:

```text
kubernetes.namespace:"argocd-hooks" and kubernetes.pod.name:tf-presync-<app>-<stage>-*
```

Each attempt shows as its own `kubernetes.pod.name` (the Job name plus a
five-character suffix); sort by `@timestamp`. Where the Kubernetes metadata is
missing, match on the file instead:
`log.file.path:*tf-presync-<app>-<stage>*`.

What is verified (2026-10-02, ANS-164): Filebeat's own log shows it harvesting
the hook pods' files, `/var/log/containers/tf-presync-<app>-<stage>-<suffix>_argocd-hooks_terraform-*.log`,
before the pod is deleted. Reading them back from Elastic is not yet proven:
it was not done for want of a credential. The check owed is one query in
Kibana for `tf-presync-fieldnotes-prd-sx956`, harvested 2026-10-01 18:26Z.
Until it returns that pod's lines, treat this route as unconfirmed.

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
GitHub token can list, create and delete hooks (`cexec iac gh api
repos/pvginkel/<repo>/hooks`). What a session lacks is the shared secret: it
reads that value only with the operator's permission for that path, and without
it the hook is made in the GitHub UI.

A delivery that worked leaves three traces: the relay logs `delivery <id>
event=push: all 1 receivers accepted`; argocd-server logs `Received push event
repo: … refreshing app from webhook`; the Application's `status.sync.revision`
moves within about ten seconds. GitHub's *Recent Deliveries* shows the relay's
response, and a `502` means argocd-server refused it or was unreachable.

Without a webhook, refresh by hand — the UI's *Refresh* button, or:

```sh
cexec iac kubectl $KC annotate application -n argocd-prd <app> argocd.argoproj.io/refresh=hard --overwrite
```

A hard refresh takes a few seconds; reading `status.sync.revision` immediately
returns the previous value.

## Rotating a token or secret

Argo's own credentials, the leaves in the table above:

1. Write the new value to its leaf, from stdin so it never lands in a history:

   ```sh
   printf %s "$VALUE" | bao kv put -mount=kv eso/prd/argocd-hooks/git token=-
   ```

2. ESO refreshes within the hour. To force it:

   ```sh
   cexec iac kubectl $KC annotate externalsecret -n <namespace> <name> force-sync=$(date +%s) --overwrite
   ```

   Each leaf's ExternalSecret is in the table above.
3. Argo reads its repo-creds Secret live; the hook reads its Secret at Job
   start, so the next sync uses the new value. To check a token without reading
   it, run a throwaway pod on the hook image with `envFrom` the Secret and print
   only the status code of `GET https://api.github.com/user` (401 vs 200), then
   let `--rm` delete it.

### The `kubecoder` account's token

Argo issues this token rather than reading it. The `kubecoder` account has no
password: an admin mints its API token, with no expiry, like KubeCoder's
Jenkins token. The token exists only in OpenBao, under `argocd-token` in
KubeCoder's prd catalog leaf `eso/prd/kubecoder/prd/catalog`. ESO extracts that
leaf whole into `kubecoder-secret-catalog` in `kubecoder-prd`, and
KubeCoderDeploy's prd values project the key onto every prd environment as
`ARGOCD_AUTH_TOKEN`, read at pod start. Once KubeCoderDeploy names the key,
every prd environment's start fails while the leaf lacks it.

One procedure mints the first token and rotates it after. It runs from a prd
environment as written, or without `cexec iac` from any machine with the CLI
and `bao`. The CLI takes `ARGOCD_AUTH_TOKEN` over a logged-in context, and
every prd environment carries it, so each admin step clears it with `env -u`.
Without that, the step acts as the read-only account and is refused.

1. Log in as the local `admin`, with the password from
   [Break-glass](#break-glass-the-local-admin-account-d9). SSO cannot log the
   CLI in: the Keycloak client `argocd` is confidential, and the CLI's login
   sends no client secret.

   ```sh
   cexec iac env -u ARGOCD_AUTH_TOKEN argocd login argocd.home --grpc-web --username admin
   ```

2. List the account's tokens and note their ids, which step 6 revokes. The
   first mint finds none.

   ```sh
   cexec iac env -u ARGOCD_AUTH_TOKEN argocd account get --account kubecoder
   ```

3. Mint a token and pipe it straight into the leaf, so it never reaches a
   terminal or a history. `generate-token` sets no expiry by default. `patch`,
   never `put`: the leaf holds every other catalog key, and `bao kv put`
   replaces a leaf whole. `tr` drops the newline the CLI prints after the
   token, which would otherwise become part of the variable.

   ```sh
   cexec iac env -u ARGOCD_AUTH_TOKEN argocd account generate-token --account kubecoder \
     | tr -d '\n' | cexec iac bao kv patch -mount=kv eso/prd/kubecoder/prd/catalog argocd-token=-
   ```

4. Log out. The saved context is an admin session, and the environment's home
   is shared with every container in the pod:

   ```sh
   cexec iac env -u ARGOCD_AUTH_TOKEN argocd logout argocd.home
   ```

5. ESO refreshes within the hour. To force it:

   ```sh
   cexec iac kubectl $KC annotate externalsecret -n kubecoder-prd kubecoder-secret-catalog force-sync=$(date +%s) --overwrite
   ```

   A stopped environment gets the new token when it next starts; a running
   one keeps the old token until it restarts. In an environment started after
   the refresh, `cexec iac argocd account get-user-info` checks the token
   without reading it: `Logged In: true`, `Username: kubecoder`.
6. Revoke the old tokens once every environment that was running when step
   5's refresh landed — at the force-sync, or up to an hour after step 3
   without one — has restarted or stopped: one that started before the
   refresh read the old token. An environment still holding a revoked token
   loses its read view until it restarts. Log in as in step 1, revoke each id
   noted in step 2, and log out as in step 4:

   ```sh
   cexec iac env -u ARGOCD_AUTH_TOKEN argocd account delete-token --account kubecoder <id>
   ```

## Upgrading Argo CD

1. In ArgoCDDeploy, bump the exact `argo-cd` version in `chart/Chart.yaml`,
   rebuild `Chart.lock`, run the repo's render gate, push.
2. ArgoCDDeploy's relay webhook delivers the push, and `argocd-prd` goes
   OutOfSync within seconds. Review the diff in the UI. The CRDs sync
   server-side (`ServerSideApply=true` on all three).

3. Sync by hand at a chosen moment (D3). The controller and repo-server restart
   mid-sync, and every Application pauses with them.
4. Verify: `argocd-prd` Synced and Healthy, every pod Running, the full
   Application list back, and a webhook delivery accepted by the relay's receiver. If
   the list does not come back, see item 4 of Diagnosing a failed sync.
5. Move the `argocd` CLI to the same version, in DockerImages'
   `kube-coder-iac-toolchain/Dockerfile`: `ARGOCD_VERSION` is the version
   `https://argocd.home/api/version` now reports, and `ARGOCD_SHA256` the
   `argocd-linux-amd64` line of that release's `cli_checksums.txt`. The new CLI
   reaches an environment when its `iac` sidecar next starts on the published
   image.

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
`argocd-rbac-cm` (the realm uses email as username). The one other binding is
the `kubecoder` account's `role:readonly`, and `policy.default` is empty, so
any other identity gets nothing at all.

## Registering, undeploying and unregistering an app

The registry is ArgoCDDeploy's `releases/values.yaml` (D63). An app's entry sits
under `apps:`, and each of its stages becomes one Application, named and
namespaced `<app>-<stage>`:

```yaml
apps:
  <app>:
    repo: https://github.com/pvginkel/<DeployRepo>.git
    stages:
      prd: {}
```

An app whose chart comes from a Helm repository adds `upstream: {repo, chart}`,
and each of its stages pins the chart's `version`. A stage may set
`targetRevision` (default `main`), and `syncOptions` is app-level: every stage's
Application gets it (D62). The file's header comment names every key, and
`releases/values.schema.json` refuses anything else. Keep entries alphabetical.
`helm lint releases` checks an edit against the schema, and ArgoCDDeploy's
`kc project test` runs the render test. Push; the relay webhook refreshes
`releases`, whose sync creates the Application, and Argo then syncs it on its
own. A stage that takes over resources already running, as a migration's
cutover does, registers with `autoSync: false` instead: the Application is
created OutOfSync, its diff is reviewed, it is synced once by hand, and the
flag is then turned on (D5). A new app has nothing live to diff.

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
within 45 seconds on 2026-09-13. What stays is what the stage's Terraform made,
since teardown never destroys (D29): its PVs and what backs them, databases,
buckets, the deploy repo's GitHub webhook if the stage manages it. So do its
state file in TerraformState and its `config/<stage>/` in the deploy repo. One
`IaC/Destroy Stage` build deletes them all
([Destroying a retired stage](#destroying-a-retired-stage), D66).
**Unregister** is deleting the app's whole entry, which undeploys each stage the
same way.

## Destroying a retired stage

One build of
[`IaC/Destroy Stage`](https://jenkins.webathome.org/job/IaC/job/Destroy%20Stage/)
deletes what an undeployed stage left, for good: the resources in its Terraform
state, its `argocd/<repo>/<stage>/` in TerraformState and its `config/<stage>/`
in the deploy repo (D66). The build runs ArgoCDTools' `Jenkinsfile.destroy-stage`
as `jenkins-prd/destroy-stage`. The work is done by the argocd-hook image's
destroy mode (`python3 -m presync.destroy`, from `argocd-hook:latest`), run as
the Job `argocd-hooks/destroy-stage-<build#>` under `tf-presync` with
`argocd-hook-credentials`. The build shows the Job's log in its console and
deletes the Job when its stage ends.

The build cleans up after an undeploy and never undeploys. Undeploy the stage
first ([above](#registering-undeploying-and-unregistering-an-app)): delete its
registry entry, then sync `releases` with *Prune*, so that its Application goes
and its namespace with it.

*Build with Parameters* takes three:

- `REPO`: the deploy repo exactly as GitHub spells it, `FieldnotesDeploy` and
  not `fieldnotesdeploy`;
- `STAGE`: the retired stage, e.g. `dev`;
- `APPLY`: unticked by default, which makes the build a dry run.

A dry run comes first, then an apply. The apply is the operator's keystroke.

1. **Dry run** (`APPLY` unticked). The build runs `Checkout`, `Check stage is
   undeployed` and `Plan destroy`. It writes nothing: no resource, no state, no
   commit anywhere. The Job's log reads, in order:
   - `presync: <file>.tf: keeps N declaration(s), drops …`, once per root
     `.tf`: the root reduced to its `terraform`, `provider` and `variable`
     blocks. With nothing else declared, every resource in state is planned
     for deletion, and `prevent_destroy` no longer binds;
   - `presync: the clone has no config/<stage>/: planning without its tfvars`,
     only when that folder is already gone;
   - `presync: an apply forgets N namespaced Kubernetes object(s), …`, one line
     per object with its namespace, or `… nothing to forget`
     ([below](#what-the-build-forgets-rather-than-destroys));
   - Terraform's plan: a `will be destroyed` per resource, and `Plan: 0 to add,
     0 to change, N to destroy.`;
   - `presync: an apply removes argocd/<REPO>/<STAGE>/ from the state
     repository`;
   - `presync: dry run: nothing was written`.

   The build's own line follows: `An apply removes config/<STAGE>/ from
   pvginkel/<REPO>'s main. This dry run pushed nothing.` Read the plan as the
   list of what an apply deletes, with whatever data those resources hold.
   Nothing else stands in its way.
2. **Apply** (`APPLY` ticked). The build runs `Checkout`, `Check stage is
   undeployed`, `Destroy Terraform resources and state` and `Remove config
   folder`. Its Job plans again, at the SHA of `main` this build resolved, and
   applies that plan with no `input` step. The dry run's plan is what to
   expect; the log shows the plan that ran, above Terraform's apply output. The
   Job's log reads:
   - `presync: forgetting N namespaced Kubernetes object(s), …` (or `… nothing
     to forget`), which `terraform state rm` then drops;
   - Terraform's plan and apply;
   - `presync: the state lists no resources`, checked before TerraformState is
     touched;
   - `presync: removed argocd/<REPO>/<STAGE>/ from the state repository`: a
     TerraformState commit `Remove argocd/<REPO>/<STAGE>/: <STAGE> of <REPO>
     destroyed` by `argocd-hook@<pod>`.

   Once the Job has succeeded, `Remove config folder` commits `Remove
   config/<STAGE>/: <STAGE> destroyed by IaC/Destroy Stage #<n>` to the deploy
   repo's `main` as `jenkins`, and prints `pvginkel/<REPO> <sha> removes
   config/<STAGE>/.` The build reads and edits `main` only, whatever branch the
   stage tracked (D34).

Do not abort an apply while its Job runs
([A build that stopped halfway](#a-build-that-stopped-halfway)).

### What the guard refuses

`Check stage is undeployed` fails the build before any Job starts:

- while the stage is still deployed, by an entry in ArgoCDDeploy's
  `releases/values.yaml` on `main` that deploys `REPO`'s `STAGE`, or by a live
  Application in `argocd-prd` that sources `REPO` with the Helm parameter
  `hook.stage` set to `STAGE`, single- or multi-source. Repo URLs match
  ignoring case, `.git` and a trailing `/`. The failure names both checks'
  findings:

  ```text
  prd of pvginkel/FieldnotesDeploy is still deployed, by the registry entry apps.fieldnotes.stages.prd in ArgoCDDeploy's releases/values.yaml on main and the live Application argocd-prd/fieldnotes-prd. This build cleans up after an undeployed stage: delete its registry entry and prune its Application first.
  ```

- when `REPO` is not spelled as GitHub spells it: `GitHub spells
  pvginkel/fieldnotesdeploy as FieldnotesDeploy, the spelling the stage's state
  is filed under: run with REPO=FieldnotesDeploy`. GitHub serves a repo under
  any case of its name, but TerraformState's paths are case-sensitive: another
  case finds no state, and an apply would still remove `config/<stage>/`;
- when `REPO` or `STAGE` is empty or not a single path segment. A build without
  parameters fails here on the empty `REPO` and changes nothing; that is how
  the job's first build registered its parameters.

The guard does not check whether the stage's namespace is gone.

### What the build forgets rather than destroys

The prune took the stage's namespace with everything in it, the
`tf-presync-app` RoleBinding included. That RoleBinding is the Job's only grant
in a namespace (D33), so Terraform reading such an object now gets `403`. The
Job therefore drops every Kubernetes object in state that lives in a namespace
from the state, without deleting it: the dry run lists them as `an apply forgets
…`, the apply as `forgetting …`. Cluster-scoped objects such as PVs, and
everything outside Kubernetes (RBD images, ZFS datasets, databases, buckets, the
webhook), are destroyed for real. `tf-presync` gets no grant for this.

A namespace that outlived its Application keeps those objects, its Secrets
among them: the build forgets them all the same, and they stay behind (D66's
accepted risk). Before the build, `cexec iac kubectl get namespace
<app>-<stage>` should answer `NotFound`. If it does not, removing that namespace
is a separate act, by hand.

### A build that stopped halfway

Run it again with the same parameters. Each step skips what is already done:

- an empty state skips the destroy (`presync: the state lists no resources to
  destroy`);
- a state that is already gone skips the destroy too (`presync: the backend
  holds no state at argocd/<REPO>/<STAGE>/terraform.tfstate: nothing to
  destroy`), and no run creates one;
- a folder already gone from TerraformState is skipped (`presync: the state
  repository has no argocd/<REPO>/<STAGE>/: nothing to remove`);
- once `config/<stage>/` is gone, the Job plans without its tfvars, and
  `Remove config folder` prints `pvginkel/<REPO> has no config/<STAGE>/ on main:
  nothing to remove.`

So a build whose `Remove config folder` failed is finished by a re-run whose Job
skips everything, and a Job that ended with `presync: the state still lists …:
its folder stays` is finished by a re-run that plans what is left.

The build deletes its Job however it ends, an abort included, and Terraform is
then killed where it stands, without a clean shutdown. An apply cut off that way
can leave the state's lock held: the re-run then fails with Terraform's `Error
acquiring the state lock`. The lock is the branch
`locks/argocd/<REPO>/<STAGE>/terraform.tfstate` in TerraformState, and
force-unlocking means deleting that branch (AnsibleSpecs `decisions.md`,
"Concurrency control"). The Job's deadline, 30 minutes, kills it the same way,
and the build then fails in `waitForJobContainer` with no Job log in the
console. Its log may still be in Kibana, as `kubernetes.namespace:"argocd-hooks"
and kubernetes.pod.name:destroy-stage-<build#>-*`
([A replaced hook's log](#a-replaced-hooks-log-kibana); unconfirmed, as that
section says).

### A build that fails at the plan

The Job inits and plans on the root's declarations alone. Its inputs are the
hook's credentials, `TF_VAR_stage` and, while `config/<stage>/` exists, the
stage's tfvars; the stage's namespace is not one of them. A root that needs more
fails the Job at `init` or
`plan`, before anything is written, and destroys nothing. The build's last line
is the Job's `presync: …`, and Terraform's error above it names the cause:

- `No value for required variable`: the root's declarations do not plan on
  their own, and the fix is in the deploy repo. KubeCoderDeploy's do not, as of
  2026-10-03: its `terraform/variables.tf` declares `namespace` with no default,
  and once `config/<stage>/` is gone, `zfs_dataset`, `zfs_quota`, `zfs_size` and
  `manage_webhook` have none either;
- anything else, from the state backend or from refreshing a resource still in
  state (Ceph, GitHub, the cluster), is the infrastructure, not the
  declarations.

A root `*.tf.json`, or a top level the reduction cannot read, fails the Job the
same way, naming the file and line.

### The webhook

A deploy repo's GitHub webhook belongs to the one stage whose tfvars set
`manage_webhook = true` ([Webhooks](#webhooks), D39). It is destroyed with that
stage, and destroying any other stage leaves it. FieldnotesDeploy's `dev` does
not manage it. KubeCoderDeploy's `dev` does, and its `prd` sets `false`.
Destroying `dev` while `prd` stays deployed would delete the repo's only hook,
and the guard does not refuse that. `prd`'s pushes then reach Argo only through
the 30-minute refresh (D6). To give the surviving stage the hook, set
`manage_webhook = true` in its tfvars after the destroy, in a commit on `main`.
A stage that tracks another branch gets that commit through its promotion, as
it gets every change (D35): KubeCoderDeploy's `prd` through
`KubeCoder/Promote-PRD`, never by a commit to `prd` itself, which that job's
fast-forward would then refuse. Once the commit is on the branch the stage
tracks, refresh its Application by hand, since no hook delivers that push. Its
sync's apply creates the hook. Not before the destroy: while the old
hook exists, that create fails on GitHub's hook-already-exists.

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
| `PipelinesDeploy` | `pipelines-deploy` | prd, from `main` | a new app: no current producer |

What the deploy repo carries:

- **`architecture.yaml`** at the root: the judgment layer. Its schema is what
  `gen-architecture --help` prints from the aac-tools toolchain (`cexec aac-tools
  gen-architecture --help`).
- **`Jenkinsfile.architecture`**: the Jenkins pipeline style guide's deploy-repo
  producer (<https://pipelines.home/docs/types/deploy-architecture/>) with its
  header, stage and producer id changed. It declares its own push trigger and
  concurrency guard, and checks out the branch the job builds (`checkout scm`).
  Its stages call the library's `architectureProducer` steps
  (<https://pipelines.home/docs/reference/architectureProducer/>):
  `generate(stage: '<stage>', producer: '<app>-deploy')` runs `gen-architecture`
  and `archive` archives `docs/architecture/*.yaml`, then `validate` runs
  `arch-validate docs/architecture/*.yaml` as the gate. `generate` and
  `validate` run in the `aac-tools` container, which the pod declares with
  `podYaml(templates: ['aac-tools'])`. The collector copies only `.yaml` files
  under an `architecture/` directory, from the job's last successful build, and
  `archive` refuses a pattern it would not copy.
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
  them alternately. The job builds the branch Argo syncs the stage from, and
  `Jenkinsfile.architecture` passes that stage to `--stage`, which is required
  and takes one value. That is the whole guard. The generator does not read the
  branch, and it has no rule about which stages an app publishes. KubeCoder
  publishes prd only.
- **The annotation file states `introduced:`.** A deploy repo's history dates
  the repo, not the app, so the generator requires the key and has no fallback.
  A new app takes the date of the first commit that adds its deploy repo's
  `chart/`, as ArgoCDDeploy's does. An annotation file copied from HelmCharts'
  `charts/<app>/architecture.yaml` keeps HelmCharts' date, the first commit that
  adds `charts/<app>`. Every published element of the app carries that date:

  ```sh
  # HelmCharts is archived: git clone https://github.com/pvginkel/HelmCharts /work/scratch/HelmCharts
  git -C /work/scratch/HelmCharts log --diff-filter=A --reverse --format=%ad --date=short -- charts/<app> | head -1
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
    The artifact is build output; edit only the judgment layer, architecture.yaml,
    whose schema is what gen-architecture --help prints from the aac-tools toolchain.
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

The order runs from a committed producer to a registered one:

1. Commit the files. `kc project test` in the deploy repo generates and
   validates the artifact. Two regenerations from the same commit must be
   byte-identical. A difference means render-time randomness reaches an id or
   an emitted field.
2. Create the Jenkins job `AaC/<Repo>` through the Jenkins API, with the push
   trigger in its `config.xml`: the style guide's new-repo recipe
   (<https://pipelines.home/docs/guide/new-repo/>). Jenkins installs the repo's
   push hook when the job is created. Creating the job starts no build.
3. Push the published branch. The push starts the job's first build, and the
   first green build archives `docs/architecture/<app>-deploy.yaml`. A promotion
   branch must exist and carry the producer. KubeCoderDeploy's `prd` is created
   from `main` by its promote job's first run, at the prd cutover.
4. Only after that green build, register the producer: commit its entry to
   `pipeline-producers.yaml` on `main` in `pvginkel/Architecture`:

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
   ping, or a redelivery, logs "all 1 receivers accepted" at the relay.
7. Delete the release record step 3's `helm install` left. Argo renders with
   `helm template` and never reads it, but while it exists `helm list -A` shows
   Argo CD as a Helm release, and a stray `helm upgrade` or `helm uninstall`
   would act on Argo CD itself:

   ```sh
   cexec iac kubectl $KC -n argocd-prd delete secret sh.helm.release.v1.argocd-prd.v1
   ```

8. Mint the `kubecoder` account's token anew:
   [its procedure](#the-kubecoder-accounts-token), steps 1 to 5. Step 2 finds
   no ids, so there is nothing to revoke. The rebuild recreated
   `argocd-secret`, whose `server.secretkey` signs the account's tokens and
   which lists their ids, so Argo refuses the token every prd environment
   holds. A running environment keeps the refused token until it restarts.

## Known behaviours

- **A manual sync by `kubectl patch` inherits the last operation's fields.** A
  patch that sets `.operation` without `sync.revision` or `sync.resources` does
  not clear them: the controller keeps the previous operation's values in
  `status.operationState.operation`. On an app with a PreSync hook (`argocd-prd`'s
  redis-secret-init), it resumes from that copy after the hook, so the sync runs
  at the old revision or on the old resource list, and still ends `Succeeded`
  ("Partial sync operation", or `configured` with nothing written). Seen at the
  registry switch on 2026-09-28. What worked there: set `sync.revision` to the
  SHA and list `sync.resources` explicitly. Whether a full sync by patch can
  shed an earlier partial one's list is untested; the UI's Sync is the
  alternative. Check the objects afterwards, not only the phase.
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
- **Every webhook-relay build leaves `argocd-prd` OutOfSync.** DockerImages writes each relay
  build's number into ArgoCDDeploy's `config/prd/values.yaml` (`relay.image`), the version
  poller's scheduled rebuilds included, and Argo's own Application never syncs on its own (D3).
  The relay runs its previous build until you sync `argocd-prd` by hand; the diff is the relay
  Deployment's image.
- **One hook Job per app, latest attempt only.** The fixed name
  `tf-presync-<app>-<stage>` lets `BeforeHookCreation` replace it on every sync
  and retry (ANS-137), so an earlier attempt's log is only in Kibana (above).
  The Job goes with the Application.
- **Rebuilds.** Argo runs on prd only, deploys only into prd (`in-cluster`) and
  keeps no node-local state, so neither a prd node rebuild nor a `srvk8sdev`
  rebuild touches it.
