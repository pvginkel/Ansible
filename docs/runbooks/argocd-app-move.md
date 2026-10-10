# Moving an app into a deploy monorepo

The deploy-repo consolidation (argo-cd D11 as revised 2026-10-07) moves each app deployed from an
upstream Helm chart into a directory of `PlatformAddOnsDeploy`, and each app with a chart of its own
and no source repository of its own into a directory of `HomelabAppsDeploy`. This page moves one
app. It is the design's ten-step per-app move
([`deploy-repo-consolidation.md`](../../../AnsibleSpecs/argo-cd/deploy-repo-consolidation.md),
§Per-app move) as slice 057's rulings D1 and D2 shape it. Slice 057 runs it for headlamp and
pgadmin, slices 058 and 059 for the rest. What a moved app then is: its registry entry in
[argocd.md](argocd.md#registering-undeploying-and-unregistering-an-app), its producer in
[argocd.md](argocd.md#an-app-in-a-monorepo).

**Every step is Claude's; the operator answers the stops.** Slice 057's ruling D1, which holds for
058 and 059, gives Claude the whole move: the pushes, the state move, the plan Job, the registry
switch, the sync, the job deletion and the archive. It is the one exception to
[argocd.md](argocd.md#conventions)'s operator keystroke, and it reaches no other sync. A stop halts
the move where the table below says and leaves what it says; the operator decides what follows. A
slice's run hands a stop back as its verdict `question`.

## The stops

| Stop | Fires when | The move is left |
| --- | --- | --- |
| The render (step 1) | an object other than the hook's Job and RoleBinding and the ConfigMap `tf-presync-revision` differs between the two renders | before the state rename: nothing has changed for Argo |
| Rule (1), the plan (step 4) | the plan at the new key lists anything but `github_repository_webhook.argocd[0]: destroy`, or fails | the rename reverted ([before the registry switch](#before-the-registry-switch)) before the registry is touched |
| Rule (2), the diff (step 6) | the Application's diff after the switch shows anything but `tf-presync-revision`'s data | parked: the switch and the rename stay, `autoSync` stays off, nothing applies |
| A proof (steps 8, 10) | the producer's artifact or the model changes; the annotation proof fails | where it fails: an app that runs from the monorepo stays there |

Rule (2) parks rather than reverts: with the rename reverted, the switched Application's state
would sit at the old key while it applies at the new one. Rule (1) names the old repo's relay
webhook, which the old stage's Terraform holds when its tfvars set `manage_webhook = true`. A stage
that does not set it has no webhook to destroy, and its plan is a stop until the operator rules on
it.

## Before a move

The move needs slice 056's tooling (homelab-shared 0.6.0, the registry's `path:`, Architecture's
`path:` producers), an argocd-hook build with the plan mode (IaC/ArgoCDTools #38 on, as `:latest`),
and the monorepo with its `AaC/<Monorepo>` job and its hand-made relay webhook. Every command on
this page reads these, set once per move:

```sh
APP=headlamp STAGE=prd                        # the registry key, the stage
OLD=HeadlampDeploy MONO=PlatformAddOnsDeploy  # the app's deploy repo, the monorepo it moves into
NS=$APP-$STAGE                                # hook.namespace: the Application's name and namespace
REC=/work/AnsibleSpecs/slices/<slice>         # the move's record: renders, logs, the old job's config
KC="--kubeconfig $HOME/.kube/config-prd-write --context prd"
J="-u $JENKINS_USER:$JENKINS_TOKEN"
```

The clones, each brought to `origin/main` before its step: `/work/scratch/$OLD`,
`/work/scratch/$MONO`, `/work/scratch/TerraformState` (`git clone
https://github.com/pvginkel/TerraformState /work/scratch/TerraformState` the first time),
`/work/ArgoCDDeploy`, `/work/Architecture` and `/work/DockerImages`. Each read's output goes into
`$REC`, named for what it is.

An app with two stages (keycloak's dev and prd) moves both at once, since its registry entry is
one: steps 3, 4 and 6 and the reads of step 7 run for each stage, and step 5 sets
`autoSync: false` on each.

## Where a move stands

A move opens with this read, and so does every return to it: after a timeout, an answered stop, a
review round. It resumes at the first step whose *done when* does not hold, so no production step
runs twice or is skipped. The render, the plan and the diff are reads: a return runs them again and
never takes an earlier round's output.

```sh
raw() { gh api "repos/pvginkel/$1/contents/$2" -H 'Accept: application/vnd.github.raw'; }
has() { gh api "repos/pvginkel/$1/contents/$2" --silent 2>/dev/null && echo "$1 $2: present" || echo "$1 $2: absent"; }
# 1: the app's directory on the monorepo's main, and AaC/<Monorepo>'s last green build
has $MONO $APP
curl -s $J "$JENKINS_URL/job/AaC/job/$MONO/lastSuccessfulBuild/api/json?tree=number,artifacts%5BrelativePath%5D" | jq -c
# 2: deploy-pins.json entries that still name the old repo
git -C /work/DockerImages fetch -q origin && git -C /work/DockerImages grep -l "\"pvginkel/$OLD\"" origin/main -- '*/deploy-pins.json'
# 3: the state at each key, and any lock held on either
for key in argocd/$OLD/$STAGE argocd/$MONO/$APP/$STAGE; do
  has TerraformState $key/terraform.tfstate
  gh api repos/pvginkel/TerraformState/git/matching-refs/heads/locks/$key/ --jq '.[].ref'
done
# 5, 7: the registry entry on ArgoCDDeploy's main
raw ArgoCDDeploy releases/values.yaml | yq -c --arg app $APP '.apps[$app]'
# 5-7: the live Application, and the state key its hook's last run read
cexec iac kubectl get application -n argocd-prd $NS -o json | jq -c '{sync: .status.sync.status,
  health: .status.health.status, revision: (.status.sync.revision // .status.sync.revisions),
  repos: ([.spec.source // empty, (.spec.sources // [])[]] | map(.repoURL) | unique),
  autoSync: (.spec.syncPolicy.automated != null)}'
cexec iac kubectl logs -n argocd-hooks job/tf-presync-$NS | grep -m1 'Getting state from' | sed 's/.*\/\///'
# 8: the producer's entry on Architecture's main
raw Architecture pipeline-producers.yaml | yq -c --arg id $APP-deploy '.producers[] | select(.id == $id)'
# 9: the old job (200 while it exists, 404 once deleted) and the old repo
curl -s -o /dev/null -w "AaC/$OLD: %{http_code}\n" $J "$JENKINS_URL/job/AaC/job/$OLD/api/json"
gh api repos/pvginkel/$OLD --jq '"archived: \(.archived)"'
```

| Step | Done when |
| --- | --- |
| 1. The directory | `$MONO $APP: present` |
| 2. The pins | no `deploy-pins.json` names `pvginkel/$OLD` |
| 3. The state rename | the new key `present`, the old `absent`, no lock on either |
| 4. The plan | a read: run while step 5 is not done |
| 5. The registry switch | the entry names `$MONO` and `path: $APP` |
| 6. The diff | a read: run while the entry carries `autoSync: false` |
| 7. The sync | the entry has no `autoSync`; the Application reads `$MONO`, `autoSync: true`, `Synced` and `Healthy`; its hook's last run read `argocd/$MONO/$APP/$STAGE/terraform.tfstate` |
| 8. The producer | the entry names `pvginkel/$MONO`, `AaC/$MONO` and `path: $APP` |
| 9. The old job and repo | `AaC/$OLD: 404` and `archived: true` |
| 10. The annotation proof | the pilots only: their slice's record says |

## The move

### 1. The app's directory

```sh
cd /work/scratch/$MONO && git fetch origin && git merge --ff-only origin/main
# git subtree needs GNU dirname: uutils coreutils' (Ubuntu 25.10, the KubeCoder dev container)
# prints `.` for `dirname $APP/.`, and the add fails with `invalid path './<file>'`.
mkdir -p /tmp/gnubin && ln -sf /usr/bin/gnudirname /tmp/gnubin/dirname
PATH=/tmp/gnubin:$PATH git subtree add --prefix=$APP https://github.com/pvginkel/$OLD.git main
```

The subtree brings `$OLD`'s history under `$APP/`. Then one commit:

- `$APP/chart/Chart.yaml`: `homelab-shared` to `0.6.0`, the first version that hands the hook
  `hook.path`; `cexec aac-tools chart-deps --repo $APP --update` rewrites `$APP/chart/Chart.lock`.
- `$APP/config/$STAGE/terraform.tfvars`: `manage_webhook = false` where it was `true`. The
  monorepo's relay webhook is hand-made; the old repo's goes with the sync's apply (step 7).
- `$APP/Jenkinsfile.architecture`, `$APP/.architecturerc`, `$APP/.kubecoder/` and `$APP/.gitignore`
  deleted: the monorepo's root files serve every app.
- `.kubecoder/project.yaml`: a project `$APP`. A project's key is its folder and its statements'
  working directory, so the old repo's `lint` and `test` statements carry over with `hook.repo`
  naming `$MONO` and `hook.path=$APP` beside it.

`kc project lint` and `kc project test` green from `/work/scratch/$MONO` (every project, `$APP`
among them), then commit.

**The prediction**, before the push: render both sides as their Applications do and compare them
object by object.

```sh
OLDREV=$(git -C /work/scratch/$OLD rev-parse origin/main)   # what the Application synced last
NEWREV=$(git -C /work/scratch/$MONO rev-parse HEAD)         # the step-1 commit
mkdir -p $REC/render-$NS
cexec aac-tools chart-deps --repo /work/scratch/$OLD
cexec aac-tools chart-deps --repo /work/scratch/$MONO/$APP
cexec iac sh -c "cd /work/scratch/$OLD && helm template $NS chart --namespace $NS \
  --set-string hook.repo=https://github.com/pvginkel/$OLD.git,hook.revision=$OLDREV,hook.stage=$STAGE,hook.namespace=$NS" \
  > $REC/render-$NS/before.yaml
cexec iac sh -c "cd /work/scratch/$MONO/$APP && helm template $NS chart --namespace $NS \
  --set-string hook.repo=https://github.com/pvginkel/$MONO.git,hook.revision=$NEWREV,hook.stage=$STAGE,hook.namespace=$NS,hook.path=$APP" \
  > $REC/render-$NS/after.yaml
```

A local app's Application passes its stage values to the chart: add `--values
config/$STAGE/values.yaml` to both `helm template`s. An upstream app's renders the upstream chart
beside the companion `chart/`, with the stage values; append it to each side, at the chart and
version its registry entry names:

```sh
for side in before:/work/scratch/$OLD after:/work/scratch/$MONO/$APP; do
  cexec iac helm template $NS <chart> --repo <upstream repo> --version <version> --namespace $NS \
    --values ${side#*:}/config/$STAGE/values.yaml >> $REC/render-$NS/${side%%:*}.yaml
done
```

Then the objects whose content differs, or which one side lacks:

```sh
objs() { yq -c 'select(. != null) | {k: "\(.kind)/\(.metadata.name)", v: .}' "$1" | sort; }
diff <(objs $REC/render-$NS/before.yaml) <(objs $REC/render-$NS/after.yaml) \
  | grep '^[<>]' | cut -c3- | jq -r .k | sort -u | tee $REC/render-$NS/differs.txt
```

It lists `ConfigMap/tf-presync-revision` and `Job/tf-presync-$NS`, and `RoleBinding/tf-presync`
where the library bump changed it. The Job and
the RoleBinding are PreSync hooks, which Argo leaves out of its diff (Charts
`charts/homelab-shared/templates/_tf-presync-hook.tpl`). The ConfigMap is the one ordinary object,
its `data.revision` going from `$OLDREV` to the monorepo's SHA. So rule (2) expects that data
alone, however far the move jumps the library. **Any other object is a stop**: rule (2) would fire
after the switch, so the move stops here instead, before the state rename, and the pushed
directory deploys nothing.

Then `git push origin HEAD:main`: a slice's run commits on its phase branch, where `git push
origin main` pushes the untouched local `main` and reports `Everything up-to-date`. The push
starts `AaC/$MONO`, which step 8 waits on.

### 2. The image pins

Each DockerImages `deploy-pins.json` entry that names `pvginkel/$OLD` (the read above) takes
`repo: pvginkel/$MONO` and its own `file` under `$APP/` (`config/dev/values.yaml` becomes
`$APP/config/dev/values.yaml`). Commit, push. From here an image
build writes its pin into the directory, where nothing deploys it until step 7. Empty for both
pilots: no `deploy-pins.json` names headlamp or pgadmin.

### 3. The state rename

```sh
cd /work/scratch/TerraformState && git pull --ff-only
gh api repos/pvginkel/TerraformState/git/matching-refs/heads/locks/argocd/$OLD/$STAGE/ --jq '.[].ref'
mkdir -p argocd/$MONO/$APP && git mv argocd/$OLD/$STAGE argocd/$MONO/$APP/$STAGE
git commit -m "argocd/$OLD/$STAGE → argocd/$MONO/$APP/$STAGE: $APP moves into $MONO"
git push origin main
```

The lock read prints nothing; a lock is a hook run in flight, so wait for it and read again. A
rejected push is a hook's state write that landed first: `git pull --rebase`, read the lock again,
push. From the push until step 5, nothing commits to `$OLD` ([Why the order
holds](#why-the-order-holds)).

### 4. The plan at the new key: rule (1)

[argocd.md's plan sheet](argocd.md#planning-a-stages-terraform-as-its-hook-applies-it), with
`REPO=$MONO APP_PATH=$APP STAGE=$STAGE NS=$NS LOG=$REC/plan-$NS.log`, at the head of the
monorepo's `main`. The verdict holds exactly one change:

```text
presync: planning against argocd/<Monorepo>/<app>/<stage>/terraform.tfstate: nothing is applied
…
presync: the plan carries 1 change(s):
presync:   github_repository_webhook.argocd[0]: destroy
presync: plan only: nothing was applied and no volume was reattached
```

The destroy is the old repo's relay webhook, which `manage_webhook = false` lets go. Planning it
at all shows that the state is at the new key and that its sops encryption does not bind the path.
**Anything else is rule (1)'s stop**: another change, no change, a failed plan. Revert the rename
([before the registry switch](#before-the-registry-switch)) before anything else, then stop.

### 5. The registry switch

ArgoCDDeploy's `releases/values.yaml`, the app's entry: `repo:` names
`https://github.com/pvginkel/$MONO.git`, `path: $APP` (the render test refuses any other
directory), and each stage takes `autoSync: false`. `kc project lint` and `kc project test` green
in `/work/ArgoCDDeploy`, commit, push. `releases` syncs the push on its own:

```sh
cexec iac kubectl get application -n argocd-prd releases -o jsonpath='{.status.sync.status} {.status.sync.revision}{"\n"}'
```

Once it reads `Synced` at the push, the Application reads the monorepo with no
`syncPolicy.automated` (the Application read above). Nothing syncs.

### 6. The diff: rule (2)

Argo refreshes an Application whose spec changed. Once the Application read shows the monorepo's
SHA as its revision:

```sh
cexec iac kubectl get application -n argocd-prd $NS \
  -o jsonpath='{range .status.resources[?(@.status=="OutOfSync")]}{.kind}/{.name}{"\n"}{end}'
cexec iac argocd app diff $NS | tee $REC/diff-$NS.txt
```

The first lists `ConfigMap/tf-presync-revision` alone, and the diff, which exits `1` because it
shows one, changes that ConfigMap's `data.revision` from `$OLDREV` to the monorepo's SHA and nothing
else: what step 1's render predicted. **Anything else parks the move** (rule (2)): the switch and
the rename stay, `autoSync` stays off, nothing applies, and the operator decides. [After the switch,
before the sync](#after-the-switch-before-the-sync) is the way back if that is the answer.

### 7. The sync

The sync is `autoSync` turned back on: D1 merges the design's steps 6 and 7. A sync by a patched
`.operation` is untested for a full sync ([Known behaviours](argocd.md#known-behaviours)). Drop each
stage's `autoSync: false` from the entry, test, commit, push. `releases` gives the Application its
`syncPolicy.automated`, and Argo syncs it, OutOfSync at a revision it has not synced. The hook Job,
`tf-presync-$NS` (`tf-presync-<hook.namespace>`), clones the monorepo at that SHA and applies
`$APP/terraform/` at the new key.

```sh
cexec iac kubectl get application -n argocd-prd $NS \
  -o jsonpath='{.status.operationState.phase} {.status.sync.status} {.status.health.status}{"\n"}'
cexec iac kubectl logs -n argocd-hooks job/tf-presync-$NS > $REC/sync-$NS.log
grep -m1 'Getting state from' $REC/sync-$NS.log; grep 'Resources:' $REC/sync-$NS.log
gh api repos/pvginkel/$OLD/hooks --jq '.[] | "\(.id) \(.config.url)"'
```

`Succeeded Synced Healthy`; the hook read `…//argocd/$MONO/$APP/$STAGE/terraform.tfstate` and
reports `Resources: 0 added, 0 changed, 1 destroyed.`; `$OLD`'s hooks no longer list
`https://deploy-hooks.webathome.org/api/webhook`, only its Jenkins one.

### 8. The producer

`AaC/$MONO` has a green build that archived `$APP/docs/architecture/$APP-deploy.yaml` (the read
above): step 1's push started it. The producer proof is that its artifact is, byte for byte, the
one the old job published:

```sh
curl -sf $J "$JENKINS_URL/job/AaC/job/$OLD/lastSuccessfulBuild/artifact/docs/architecture/$APP-deploy.yaml" | sha256sum
curl -sf $J "$JENKINS_URL/job/AaC/job/$MONO/lastSuccessfulBuild/artifact/$APP/docs/architecture/$APP-deploy.yaml" | sha256sum
```

Equal sums, or the producer proof stops the move. Then Architecture's `pipeline-producers.yaml`:
the producer's entry takes the monorepo shape ([argocd.md](argocd.md#an-app-in-a-monorepo)),
`repo: pvginkel/$MONO`, `jenkinsJob: AaC/$MONO`, `path: $APP`. Commit, push, and follow the build
the push starts (`track_build.py --hash <sha> AaC/Architecture`). It goes green, and the model is
unchanged: the producer's collected input is the same bytes in that build `B` as in the last green
build before it, `A`.

```sh
for b in $A $B; do
  curl -sf $J "$JENKINS_URL/job/AaC/job/Architecture/$b/artifact/producer-artifacts.tgz" \
    | tar -xzO --wildcards "producer-artifacts/$APP-deploy/*$APP-deploy.yaml" | sha256sum
done
```

### 9. The old job and repo

Only after step 8's green build: until it, the collector copies the producer from `AaC/$OLD`.

```sh
curl -sf $J "$JENKINS_URL/job/AaC/job/$OLD/config.xml" > $REC/AaC-$OLD-config.xml   # a rollback's copy
curl -sf -X POST $J "$JENKINS_URL/job/AaC/job/$OLD/doDelete"
gh api -X PATCH repos/pvginkel/$OLD -F archived=true --jq .archived
```

keycloak's second job, `AaC/KeycloakDeploy-dev`, is saved and deleted the same way. The old repo's
relay webhook went with step 7.

### 10. The annotation proof

Pilots only: slice 057 reads it for both pilots in one wait after both moves, and a later move does
not repeat it. A commit to the monorepo outside `$APP/` must leave the app alone:

```sh
cexec iac kubectl get job -n argocd-hooks tf-presync-$NS -o jsonpath='{.metadata.creationTimestamp}{"\n"}'
cexec iac kubectl get configmap -n $NS tf-presync-revision -o jsonpath='{.data.revision}{"\n"}'
cexec iac kubectl get application -n argocd-prd $NS -o jsonpath='{.status.sync.status} {.status.reconciledAt}{"\n"}'
```

Read before the commit, after its webhook-driven refresh, and after at least one periodic refresh
(`timeout.reconciliation: 30m`, no jitter): the app stays `Synced`, with the Job's creation time and
the ConfigMap's revision unchanged. Never a hard refresh: it re-renders at HEAD and runs the app's
hook (argo-cd D69).

## Why the order holds

- **The pins (2) before the rename (3), and nothing commits to `$OLD` until the switch (5).** With
  `selfHeal: false` the old Application syncs only on a new commit to `$OLD`
  ([argocd.md](argocd.md#registering-undeploying-and-unregistering-an-app)). A commit in that
  window, a DockerImages pin write or a hand edit, syncs it, and its hook applies against an empty
  state at the old key.
- **The directory pushed (1) before the plan (4):** the plan clones the monorepo at its SHA.
- **The rename (3) and a clean plan (4) before the switch (5):** the switched Application's first
  sync applies at the new key, which has to hold the state by then.
- **The switch with `autoSync: false` (5), the diff (6), then `autoSync` on (7):** the diff is read
  before anything applies.
- **A green `AaC/$MONO` build before the re-point (8):** a registered producer with no archived
  artifact fails the collector's discovery, and a failed collector run publishes nothing.
- **The old job and repo (9) after the re-point's green build:** until then the collector copies
  the producer from `AaC/$OLD`.

## Rollback

Four windows, by how far the move got. A later window's steps undo the earlier steps too, in
reverse.

### Before the registry switch

Steps 3 and 4 done, 5 not; where rule (1) stops a move. Argo has seen nothing of it. Revert the
rename:

```sh
cd /work/scratch/TerraformState && git pull --ff-only
mkdir -p argocd/$OLD && git mv argocd/$MONO/$APP/$STAGE argocd/$OLD/$STAGE
git commit -m "argocd/$MONO/$APP/$STAGE → argocd/$OLD/$STAGE: $APP's move into $MONO reverted"
git push origin main
```

Then the plan sheet at the old key, `REPO=$OLD APP_PATH=`, from `$OLD`'s head: no changes. The
directory deploys nothing and stays while the operator decides; abandoning the move takes `$APP/`
and its project out of the monorepo in one commit, and the pins (2) back to `$OLD`.

### After the switch, before the sync

Step 5 done, 7 not; where rule (2) parks a move. The Application reads the monorepo with `autoSync`
off, and the state is at the new key. In this order:

1. The registry entry back to `$OLD`: its `repo:` as it was, no `path:`, `autoSync: false` still on
   each stage. Test, commit, push. Once `releases` has synced, the Application reads `$OLD` and is
   `Synced` at `$OLDREV`.
2. The state back to the old key, as [above](#before-the-registry-switch), with its plan.
3. `autoSync` on: drop `autoSync: false`, test, commit, push. Nothing syncs: the app stands at the
   revision it synced last.

`autoSync` is off throughout, so nothing applies in between.

### After the sync

Step 7 done, 9 not: the app runs from the monorepo, `$OLD` is unarchived and `AaC/$OLD` exists.
The move in reverse:

1. If step 8 is done: the producer's entry back to `repo: pvginkel/$OLD`, `jenkinsJob: AaC/$OLD`,
   no `path:`. Push; `AaC/Architecture` green.
2. The registry entry back to `$OLD` with `autoSync: false` on each stage. Push. The Application
   reads `$OLD`, and its diff (step 6's read) is the ConfigMap's `data.revision` alone, back to
   `$OLDREV`.
3. The state back to the old key, as [above](#before-the-registry-switch). Its plan from `$OLD`'s
   head carries one change, `github_repository_webhook.argocd[0]: create`: `$OLD`'s tfvars still
   set `manage_webhook = true`, and the move's sync destroyed the webhook.
4. `autoSync` on. Push. Argo syncs `$OLDREV`, and the hook's apply at
   `argocd/$OLD/$STAGE/terraform.tfstate` recreates the webhook: `1 added, 0 changed, 0 destroyed`.
5. The pins (2) back to `$OLD`, and `$APP/` and its project out of the monorepo in one commit.

### After archival

Step 9 done. Unarchive the repo and recreate its job from the saved `config.xml`, the push trigger
with it; creating a job starts no build, so build it once:

```sh
gh api -X PATCH repos/pvginkel/$OLD -F archived=false --jq .archived
curl -sf -X POST $J -H 'Content-Type: application/xml' --data-binary @$REC/AaC-$OLD-config.xml \
  "$JENKINS_URL/job/AaC/createItem?name=$OLD"
curl -sf -X POST $J "$JENKINS_URL/job/AaC/job/$OLD/build"
```

That build green, with `docs/architecture/$APP-deploy.yaml` archived, then [after the
sync](#after-the-sync), whose step 1 needs it. keycloak's `AaC/KeycloakDeploy-dev` comes back the
same way.
