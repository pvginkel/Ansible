# The registry switch

Hands the live Applications in `argocd-prd` from HelmCharts' registry, which the two
ApplicationSets `releases-local` and `releases-upstream` read, to ArgoCDDeploy's registry, which
the `releases` Application syncs. No Application is recreated, and none has its spec changed. It
runs once.

Design context: AnsibleSpecs `argo-cd/decisions.md`
([D63](../../../AnsibleSpecs/argo-cd/decisions.md): the registry is ArgoCDDeploy's `releases/`
chart, and its `values.yaml` is the registry; D64: this switch, what holds throughout it, and why
it only goes forward) and `design.md`'s "The registry". For day-to-day Argo operation, including
reading an Application without the CLI, see [argocd.md](argocd.md). Every command here is the
operator's keystroke.

## No way back

There is no way back to the ApplicationSets (D64: fix forward). Stopping before step 5's
orphan-delete leaves the estate as it was, with the ApplicationSets guarded. A failure after it is
fixed forward on `releases`. Correct the registry (ArgoCDDeploy `releases/values.yaml`) or the
chart, push, and then read and sync `releases` again. **Never revert step 7's flip.** A sync at
`releases.owner: applicationsets` recreates the ApplicationSets while `releases` still tracks the
Applications. That breaks D64's rule that the two never own them at the same moment.

## Before you start

The shorthands every step uses. The credentials are the ones
[live-infra-access.md](../live-infra-access.md) describes:

```sh
KC="--kubeconfig $HOME/.kube/config-prd-write --context prd"
# A manual sync without prune: the UI's Sync with Prune unticked
argosync() { cexec iac kubectl $KC patch application -n argocd-prd "$1" --type merge \
  -p '{"operation":{"initiatedBy":{"username":"registry-switch"},"sync":{"syncStrategy":{"hook":{}}}}}'; }
# Sync status, then the last operation's phase and message
argostate() { cexec iac kubectl $KC get application -n argocd-prd "$1" \
  -o jsonpath='{.status.sync.status} {.status.operationState.phase}: {.status.operationState.message}{"\n"}'; }
# What a sync of the Application would change: its OutOfSync resources and their health
outofsync() { cexec iac kubectl $KC get application -n argocd-prd "$1" \
  -o jsonpath='{range .status.resources[?(@.status=="OutOfSync")]}{.kind}/{.name} {.health.status}{"\n"}{end}'; }
# Every Application's name, uid and creation time
apps() { cexec iac kubectl $KC get applications.argoproj.io -n argocd-prd --no-headers \
  --sort-by=.metadata.name -o custom-columns=NAME:.metadata.name,UID:.metadata.uid,CREATED:.metadata.creationTimestamp; }
```

What must already be true:

- **Slice 029 is pushed.** ArgoCDDeploy's `origin/main` carries the registry chart (`releases/`)
  and the ApplicationSets' guard. Ansible's `origin/main` carries the rehearsal fixtures in
  `support/registry-switch-rehearsal/`, and Argo reads them from there. Check ArgoCDDeploy:

  ```sh
  cd /work/ArgoCDDeploy && git switch main && git pull --ff-only && git status -sb && kc project test
  ```

  You should see `## main...origin/main` with nothing ahead or behind, and a green test whose output includes
  `ok: config/prd/values.yaml renders registry-switch position S1`.
- **`argocd-prd` was Synced before slice 029's push**, so the guard is the only thing its next
  sync changes. Step 4 checks this.

**The freeze.** From step 3 until step 10 is done, change no registry. That means no push to
HelmCharts' `configs/prd/` or to ArgoCDDeploy's `releases/values.yaml`, and no `argo_migrate.py
flip` or `autosync`. Step 6 catches one that slips through. Pushes to deploy repos are fine: the
Applications keep syncing throughout.

## 1. Rehearse on throwaway objects

D64 leaves one thing unproven: that an app-of-apps adopts an orphaned Application this cleanly.
The rehearsal proves it on throwaway objects before anything real moves. The throwaway
ApplicationSet `switch-rehearsal-set` generates the Application `switch-rehearsal-tst`, which
deploys one ConfigMap into the namespace `switch-rehearsal-tst`. The app-of-apps
`switch-rehearsal-parent` renders that same Application from
`support/registry-switch-rehearsal/parent/`. It has no finalizer and no sync policy, which is how
`releases` comes up at the flip. Everything runs in project `releases`, as the real objects do.
The rehearsal proves the forward path only, and it exercises `argosync` before the real syncs
use it.

```sh
cd /work/Ansible
# The child Application's uid, creation time, last sync start and spec, then its ConfigMap's uid
rehearsal() { cexec iac kubectl $KC get application -n argocd-prd switch-rehearsal-tst \
  -o jsonpath='{.metadata.uid} {.metadata.creationTimestamp} {.status.operationState.startedAt} {.spec}{"\n"}'; \
  cexec iac kubectl $KC get configmap -n switch-rehearsal-tst switch-rehearsal -o jsonpath='{.metadata.uid}{"\n"}'; }
```

**a. Create the ApplicationSet.**

```sh
cexec iac kubectl $KC apply -f support/registry-switch-rehearsal/applicationset.yaml
argostate switch-rehearsal-tst
cexec iac kubectl $KC get application -n argocd-prd switch-rehearsal-tst -o jsonpath='{.metadata.ownerReferences[*].name}{"\n"}'
rehearsal > /tmp/switch-rehearsal.before
```

You should see `Synced Succeeded: successfully synced …`. The child syncs on its own at creation;
repeat `argostate` until it reads Synced. Its owner is `switch-rehearsal-set`, and the snapshot
file holds two lines.

**b. Orphan-delete the ApplicationSet.**

```sh
cexec iac kubectl $KC delete applicationset -n argocd-prd switch-rehearsal-set --cascade=orphan
cexec iac kubectl $KC get application -n argocd-prd switch-rehearsal-tst -o jsonpath='{.metadata.ownerReferences}{"\n"}'
rehearsal | diff /tmp/switch-rehearsal.before -
```

You should see an empty owner line and no diff output. The child Application and its ConfigMap are
the same objects they were.

**c. Create the app-of-apps and read its diff.**

```sh
cexec iac kubectl $KC apply -f support/registry-switch-rehearsal/app-of-apps.yaml
argostate switch-rehearsal-parent
outofsync switch-rehearsal-parent
```

You should see no operation phase, because the app-of-apps does not sync on its own. `outofsync`
lists nothing but `Application/switch-rehearsal-tst`, and never as `Missing`. In the UI, open
`switch-rehearsal-parent` and its *App Diff*: on `switch-rehearsal-tst` the only change is
tracking metadata (the `argocd.argoproj.io/tracking-id` annotation), and nothing under `spec`. Note
the diff's exact shape, because step 8 compares `releases`' diff with it.

**d. Sync the app-of-apps.**

```sh
argosync switch-rehearsal-parent
argostate switch-rehearsal-parent
cexec iac kubectl $KC get application -n argocd-prd switch-rehearsal-tst \
  -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/tracking-id}{"\n"}'
rehearsal | diff /tmp/switch-rehearsal.before -
```

You should see `Synced Succeeded: …` and the tracking id
`switch-rehearsal-parent:argoproj.io/Application:argocd-prd/switch-rehearsal-tst`. There is no
diff output: the child is unchanged, and it has not synced again.

**e. Delete the app-of-apps.**

```sh
cexec iac kubectl $KC delete application -n argocd-prd switch-rehearsal-parent
rehearsal | diff /tmp/switch-rehearsal.before -
```

You should see the delete return at once, because the app-of-apps has no finalizer. There is still no diff output: the child
stays.

**f. Tear down.**

```sh
cexec iac kubectl $KC delete application -n argocd-prd switch-rehearsal-tst
cexec iac kubectl $KC get applicationsets.argoproj.io,applications.argoproj.io -n argocd-prd -o name | grep switch-rehearsal
cexec iac kubectl $KC get namespace switch-rehearsal-tst
```

You should see the delete return once the child's finalizer has removed the ConfigMap and the
Namespace. The `grep` prints nothing, and the namespace is `NotFound`. If anything in a–f differs,
stop: nothing real has moved. To abandon a failed rehearsal, delete whatever exists, in this order
and without `--cascade=orphan`: the ApplicationSet, the app-of-apps, then the child.

## 2. Give ArgoCDDeploy its relay webhook

ArgoCDDeploy's only GitHub hook delivers to Jenkins, so today a push to it reaches no Argo component
(D6). `releases` needs this hook before it syncs on its own (step 10). The hook is signed with the
shared secret every hook uses (D49), at the leaf named in ArgoCDDeploy `config/prd/values.yaml`'s
`credentials.leaves.webhook`. Only the operator reads it:

```sh
cd /work/Ansible && . scripts/bao-login.sh && cexec iac bao kv get -mount=kv -field=github_secret eso/prd/argocd/prd/webhook
```

On GitHub, go to ArgoCDDeploy → *Settings* → *Webhooks* → *Add webhook* and set:

- payload URL `https://deploy-hooks.webathome.org/api/webhook`;
- content type `application/json`;
- that secret;
- SSL verification on;
- just the push event;
- active.

The pod's GitHub token can read hooks but cannot create them.

```sh
cexec iac gh api repos/pvginkel/ArgoCDDeploy/hooks --jq '.[] | [.id, .config.url, (.events | join(","))] | @tsv'
cexec iac gh api repos/pvginkel/ArgoCDDeploy/hooks/<relay hook id>/deliveries --jq '.[] | [.event, .status_code] | @tsv'
```

You should see two hooks, Jenkins' and the relay's, each on `push`. The relay's deliveries show
GitHub's creation ping as `ping 200`. The relay answers `200` only when every receiver accepted.
A `401` means the secret does not match, and a `502` names the failed leg
([argocd.md](argocd.md), "Webhooks"). From here a push to ArgoCDDeploy
refreshes `argocd-prd` by webhook. Step 7's push proves it.

## 3. Check the registry against the live Applications

The check is read-only. `tools/registry-equivalence.py` renders the registry chart from the
checkout and compares each Application's spec and finalizers with the live one in `argocd-prd`.

```sh
cd /work/ArgoCDDeploy && git pull --ff-only && cexec iac tools/registry-equivalence.py
apps > /tmp/registry-switch-apps.before
```

You should see:

- exit 0;
- every line `equal`, its owner `ApplicationSet/releases-local` or
  `ApplicationSet/releases-upstream`;
- a last line of `50 rendered, 50 live in argocd-prd, 0 differing`, with the two counts equal.

`kubecoder-prd`'s line lists a live-only `argocd.argoproj.io/hydrate` annotation, which is
harmless: annotations are reported, not compared. A `DIFFERS`, `ONLY IN REGISTRY` or `ONLY LIVE`
line is drift between the two registries. Correct `releases/values.yaml` to match what is live,
push, and run this step again. `apps` records each Application's uid and creation time, which
steps 5 and 9 compare against.

## 4. Guard the ApplicationSets

Syncing `argocd-prd` at slice 029's commit adds `argocd.argoproj.io/sync-options: Prune=false` to
both ApplicationSets and changes nothing else. From then on, no sync deletes them, with prune or
without. Slice 029's push came before the webhook, so refresh by hand first:

```sh
cexec iac kubectl $KC annotate application -n argocd-prd argocd-prd argocd.argoproj.io/refresh=normal --overwrite
outofsync argocd-prd
```

You should see exactly `ApplicationSet/releases-local` and `ApplicationSet/releases-upstream`. In
the UI, `argocd-prd`'s *App Diff* shows the one added annotation on each. Anything more is a change
pending from before slice 029's push. Stop, and read and sync that on its own first
([argocd.md](argocd.md), "Upgrading Argo CD").

```sh
argosync argocd-prd
argostate argocd-prd
cexec iac kubectl $KC get applicationsets.argoproj.io -n argocd-prd \
  -o jsonpath='{range .items[*]}{.metadata.name} {.metadata.annotations.argocd\.argoproj\.io/sync-options}{"\n"}{end}'
```

You should see `Synced Succeeded: …`, then `releases-local Prune=false` and
`releases-upstream Prune=false`.

## 5. Orphan-delete both ApplicationSets

Past this step there is no way back (above).

```sh
cexec iac kubectl $KC delete applicationset -n argocd-prd releases-local releases-upstream --cascade=orphan
cexec iac kubectl $KC get applicationsets.argoproj.io -n argocd-prd
apps | diff /tmp/registry-switch-apps.before -
```

You should see both reported as `deleted`, then `No resources found in argocd-prd namespace.`, and
no diff output: every Application is still there, and each is the same object. `argocd-prd` now
shows the two ApplicationSets as missing. Leave it: a sync here would recreate them, and step 6
would then stop you.

## 6. Check again, right before the flip

```sh
cexec iac kubectl $KC get applicationsets.argoproj.io -n argocd-prd
cd /work/ArgoCDDeploy && git pull --ff-only && cexec iac tools/registry-equivalence.py
```

You should see `No resources found in argocd-prd namespace.`, then exit 0 with `… 0 differing`,
and owner `none` on every line.

- **An ApplicationSet is listed, or an owner is still `ApplicationSet/…`.** Someone synced
  `argocd-prd` after step 5. Orphan-delete them again (step 5), then repeat this step.
- **The check reports a difference.** The registry has drifted since step 3: a registration or a
  flip slipped past the freeze. Correct `releases/values.yaml` to match the live Application,
  push, and repeat this step.

## 7. Flip the owner to `releases`

```sh
cd /work/ArgoCDDeploy && sed -i 's/^  owner: applicationsets$/  owner: releases/' config/prd/values.yaml && git --no-pager diff && kc project test
```

You should see a one-line diff, `owner: applicationsets` → `owner: releases`, and a green test
whose output includes `ok: config/prd/values.yaml renders registry-switch position S3`.

```sh
git commit -am 'registry switch: releases owns the Applications (D64)' && git push && git rev-parse HEAD
cexec iac kubectl $KC get application -n argocd-prd argocd-prd -o jsonpath='{.status.sync.revision}{"\n"}'
outofsync argocd-prd
```

Do not refresh by hand. Within about ten seconds of the push, you should see:

- `argocd-prd`'s revision become the pushed SHA, which means the webhook from step 2 delivered the push;
- `outofsync` list exactly `Application/releases Missing`, with the two ApplicationSets gone from it.

If the revision does not move, the webhook is not delivering. Fix it (step 2) before going on.

```sh
argosync argocd-prd
argostate argocd-prd
cexec iac kubectl $KC get application -n argocd-prd releases \
  -o jsonpath='syncPolicy={.spec.syncPolicy} finalizers={.metadata.finalizers} operation={.status.operationState.phase}{"\n"}'
```

You should see `Synced Succeeded: …`, then `syncPolicy= finalizers= operation=`. `releases` exists
with no automated sync and no finalizer, and it has not synced.

## 8. Read `releases`' diff

```sh
argostate releases
outofsync releases
cexec iac kubectl $KC get application -n argocd-prd releases -o jsonpath='{range .status.resources[*]}{.name}{"\n"}{end}' | wc -l
```

You should see:

- no operation phase;
- `outofsync` lines naming only Applications from step 6's list, none of them `Missing`, since a
  `Missing` one would be created;
- a resource count equal to step 6's rendered count.

In the UI, open `releases` and its *App Diff*. On each Application the diff is tracking metadata
only, the shape the rehearsal's diff had in step 1c, and nothing under `spec`. If you see a spec
change, do not sync. Correct `releases/values.yaml`, push, and read the diff again: `releases`
does not sync on its own yet.

## 9. Sync `releases`

```sh
argosync releases
argostate releases
apps | diff /tmp/registry-switch-apps.before -
cexec iac kubectl $KC get applications.argoproj.io -n argocd-prd \
  -o jsonpath='{range .items[*]}{.metadata.annotations.argocd\.argoproj\.io/tracking-id}{"\n"}{end}' \
  | grep -c '^releases:argoproj.io/Application:argocd-prd/'
cd /work/ArgoCDDeploy && cexec iac tools/registry-equivalence.py | grep -v '^equal'
```

You should see:

- `Synced Succeeded: …`.
- From `apps`, one added line for `releases` itself and nothing else: every Application kept its
  uid and creation time.
- A tracking count equal to the rendered count: `releases` now tracks every Application.
- From the check, only `ONLY LIVE releases: …` and a last line whose live count is one more than
  its rendered count, with `1 differing`. The one difference is `releases`' own Application,
  which the registry does not render. Every other Application's spec is unchanged.

`releases` owns the Applications. What is left is its automated sync.

## 10. Turn on `releases`' automated sync

```sh
cd /work/ArgoCDDeploy && sed -i 's/^  autoSync: false$/  autoSync: true/' config/prd/values.yaml && git --no-pager diff && kc project test
```

You should see a one-line diff, `autoSync: false` → `autoSync: true`, and a green test whose output
includes `ok: config/prd/values.yaml renders registry-switch position S4`.

```sh
git commit -am 'registry switch: releases syncs on its own (D64)' && git push && git rev-parse HEAD
cexec iac kubectl $KC get application -n argocd-prd releases -o jsonpath='{.status.sync.revision}{"\n"}'
outofsync argocd-prd
```

Do not refresh by hand. Within about ten seconds of the push, you should see:

- `releases`' revision become the pushed SHA. A push to the registry's repo reaches `releases`
  through the webhook, and nothing polls (D6).
- `argocd-prd` list only `Application/releases`. Its *App Diff* adds a `syncPolicy`, automated with
  `prune: false` and `selfHeal: false`, and the retry block, and nothing else.

```sh
argosync argocd-prd
argostate argocd-prd
cexec iac kubectl $KC get application -n argocd-prd releases -o jsonpath='{.spec.syncPolicy}{"\n"}'
argostate releases
```

You should see:

- `Synced Succeeded: …`;
- `releases`' sync policy: automated with `prune` false and `selfHeal` false, and a retry of limit
  3 with a 30s backoff at factor 2;
- `releases` Synced.

This is steady state. A registry push reaches `releases` by webhook, and `releases` syncs on its
own. An entry deleted from the registry shows as requiring pruning until the operator prunes it (D27
as amended). The freeze ends here.

## 11. Remove the owed notes and mark the switch done

The docs already describe the new registry. Each procedure that depends on which registry is live
carries a one-line note that it is owed until the registry switch has run. To find every note:

```sh
cd /work/Ansible && git grep -n -i 'owed until the registry switch has run' -- ':!docs/runbooks/registry-switch.md'
```

Delete each hit's whole note: its paragraph or blockquote, not just the matched line. The
procedure around it stays as written. Run the grep again: it prints nothing. Commit and push.

The records in AnsibleSpecs state the switch as owed in six places:

```sh
cd /work/AnsibleSpecs && grep -n 'owed until\|until the operator has run it' argo-cd/decisions.md argo-cd/design.md argo-cd/phases.md decisions.md
```

- `argo-cd/decisions.md`: D63 ("live from the operator's registry switch (D64) and owed until
  that has run") and D64 ("it is owed until it has run").
- `argo-cd/design.md`: the opening paragraph and "The registry".
- `argo-cd/phases.md`: the endgame's registry item.
- `decisions.md`: the "Per-application TF" paragraph ("until the operator has run it").

Restate each as done, with the date the switch ran, and drop its "until then" clause. The grep
also finds D64's sequence line, which says the docs' notes are owed "until the switch has run".
That line describes this step, and it stays. Commit, staging by name: AnsibleSpecs is a shared
working tree.

This run settles slice 029's V15 and V24 (its close-out's A1 and A2).

## Dead after the switch

For the follow-up. Removing any of these changes nothing in the render.

- **In ArgoCDDeploy's chart and render test:**
  - The ApplicationSet branch, `chart/templates/applicationsets.yaml`.
  - The setting `releases.owner` (in `chart/values.yaml` and `config/prd/values.yaml`) and its
    validation in `chart/templates/releases.yaml`. `releases.autoSync` stays, true.
  - `releases.registry`, which points at HelmCharts.
  - The render test's assertions about the HelmCharts registry: `tests/render-chart.py`'s S1
    position, `check_applicationsets` and the helpers it calls, and HelmCharts in `REPOS` and
    `PERMITTED_SOURCES`.
  - `Jenkinsfile.architecture`'s header, which names HelmCharts' `configs/prd/argocd/prd/release.yaml`
    as where Argo's branch is set. That is now `releases/values.yaml`'s `apps.argocd`.
- **Also dead, though not on D64's list:**
  - `tools/registry-equivalence.py`. From the switch, `releases`' own sync status answers its
    question, and it counts `releases` itself as `ONLY LIVE`.
  - Ansible's `support/registry-switch-rehearsal/`, whose only reader is step 1.
  - [argocd.md](argocd.md)'s "Restarting the applicationset-controller", which serves only the
    ApplicationSets.
- **Serving nothing any more:**
  - HelmCharts' relay webhook.
  - The relay's applicationset-controller leg.
