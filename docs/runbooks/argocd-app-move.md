# Moving an app into a deploy monorepo

The deploy-repo consolidation (argo-cd D11 as revised 2026-10-07) moves each app deployed from an
upstream Helm chart into a directory of `PlatformAddOnsDeploy`, and each app with a chart of its own
and no source repository of its own into a directory of `HomelabAppsDeploy`. This page moves one
app. It is the design's ten-step per-app move
([`deploy-repo-consolidation.md`](../../../AnsibleSpecs/argo-cd/deploy-repo-consolidation.md),
§Per-app move) as slice 057's rulings D1 and D2 shape it. Slice 057 ran it for headlamp and
pgadmin, and slices 058 and 059 for the rest: every app of both classes has moved. What a moved
app then is: its registry entry in
[argocd.md](argocd.md#registering-undeploying-and-unregistering-an-app), its producer in
[argocd.md](argocd.md#an-app-in-a-monorepo).

**In slices 057 to 059 every step was Claude's and the operator answered the stops.** Slice 057's
ruling D1, which held for 058 and 059, gave Claude the whole move: the pushes, the state move, the
plan Job, the registry switch, the sync, the job deletion and the archive. It was the one exception
to [argocd.md](argocd.md#conventions)'s operator keystroke, and it reached no other sync. It does
not reach a later move (an app reclassified, a dedicated app retired into a monorepo): who runs
which step of one is the operator's to rule when it comes, and until they have, the keystroke
holds. A stop halts
the move where the table below says and leaves what it says; the operator decides what follows. A
slice's run hands a stop back as its verdict `question`.

## The stops

| Stop | Fires when | The move is left |
| --- | --- | --- |
| The render (step 1) | an object other than the hook's Job and RoleBinding and the ConfigMap `tf-presync-revision` differs between the two renders | before the state rename: nothing has changed for Argo |
| Rule (1), the plan (step 4) | the plan at the new key fails after its `presync: planning against` line (one that exits before it is run again once, and stops only if that one fails too), or lists anything but `github_repository_webhook.argocd[0]: destroy`; for a stage whose old tfvars do not set `manage_webhook = true`, lists anything at all | the rename reverted ([before the registry switch](#before-the-registry-switch)) before the registry is touched |
| Rule (2), the diff (step 6) | the Application's diff after the switch shows anything but `tf-presync-revision`'s data | parked: the switch and the rename stay, `autoSync` stays off, nothing applies |
| A proof (steps 8, 10) | the producer's artifact or the model changes; the annotation proof fails | where it fails: an app that runs from the monorepo stays there |

Rule (2) parks rather than reverts: with the rename reverted, the switched Application's state
would sit at the old key while it applies at the new one. Rule (1) names the old repo's relay
webhook, which the old stage's Terraform holds when its tfvars set `manage_webhook = true`. A stage
whose old tfvars do not set it (keycloak's dev: its prd holds the webhook) has none to destroy, so
rule (1)'s second expectation is that its plan carries no changes. Wherever this page expects the
webhook's destroy, or on the way back its create, such a stage expects no change. `grep
manage_webhook /work/scratch/$OLD/config/$STAGE/terraform.tfvars` says which a stage is.

## Before a move

The move needs slice 056's tooling (homelab-shared 0.6.0, the registry's `path:`, Architecture's
`path:` producers), an argocd-hook build with the plan mode (IaC/ArgoCDTools #38 on, as `:latest`),
and the monorepo with its `AaC/<Monorepo>` job and its hand-made relay webhook. Every command on
this page reads these, set once per move:

```sh
APP=headlamp STAGE=prd                        # the registry key, the stage
OLD=HeadlampDeploy MONO=PlatformAddOnsDeploy  # the app's deploy repo, the monorepo it moves into
NS=$APP-$STAGE                                # hook.namespace: the Application's name and namespace
PROD=$APP-deploy JOB=$OLD                     # the producer, and the old job that publishes it
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
one. Step 1's prediction, steps 3, 4 and 6, the reads of step 7 and the opening read's state and
Application lines run for each stage, with `STAGE` and `NS` set to it, and step 5 sets
`autoSync: false` on each. A difference that only one stage's render shows stops the move all the
same, before the rename. The dev stage has a producer of its own, `$APP-dev-deploy`, which the old
repo's second job `AaC/$OLD-dev` publishes and `AaC/$MONO` archives beside `$APP-deploy`
(keycloak's `keycloak-dev-deploy`, from `AaC/KeycloakDeploy-dev`). The opening read's producer and
job lines, step 8, step 9's job lines and the rollback windows' producer steps run for each
producer, the second time with `PROD=$APP-dev-deploy JOB=$OLD-dev`.

## Where a move stands

A move opens with this read, and so does every return to it: after a timeout, an answered stop, a
review round. It resumes at the first step whose *done when* does not hold, so no production step
runs twice or is skipped. The render, step 2's wait, the plan and the diff are reads: a return runs
them again and never takes an earlier round's output. So is step 8's model compare, the `A`/`B`
sums of `AaC/Architecture`'s builds: step 8 reads done once the re-point is pushed, before the
compare has run, so a return runs it again while the producer's old job is not deleted.

```sh
raw() { gh api "repos/pvginkel/$1/contents/$2" -H 'Accept: application/vnd.github.raw'; }
has() { gh api "repos/pvginkel/$1/contents/$2" --silent 2>/dev/null && echo "$1 $2: present" || echo "$1 $2: absent"; }
# step 2's wait: the DockerImages build running, whether the newest finished build that checked
# out DockerImages holds the pin push, whether $OLD's main has a commit the monorepo's lacks, and
# each values file the directory pins differently from $OLD
pinwait() {
  { date -u +%FT%TZ
    git -C /work/DockerImages fetch -q origin
    b=$(curl -s $J "$JENKINS_URL/job/DockerImages/api/json?tree=builds%5Bnumber,building,actions%5BlastBuiltRevision%5BSHA1%5D,remoteUrls%5D%5D%7B0,5%7D")
    jq -r '.builds[] | select(.building) | "running: #\(.number)"' <<<"$b"
    jq -r '[.builds[] | select(.building | not) | [.number, first(.actions[]
      | select(.remoteUrls[0]? == "https://github.com/pvginkel/DockerImages.git") | .lastBuiltRevision.SHA1)]
      | select(length == 2)][0] // [] | @tsv' <<<"$b" \
      | { read -r n rev; git -C /work/DockerImages merge-base --is-ancestor $(cat $REC/pins-pushed-$APP.txt) "$rev" 2>/dev/null \
          && echo "pin push: built by #$n" || echo "pin push: not built"; }
    git -C /work/scratch/$MONO fetch -q origin
    git -C /work/scratch/$MONO fetch -q https://github.com/pvginkel/$OLD.git main
    git -C /work/scratch/$MONO merge-base --is-ancestor FETCH_HEAD origin/main \
      && echo "$OLD main: in $MONO" || echo "$OLD main: ahead of $MONO"
    for f in $(git -C /work/scratch/$MONO ls-tree -r --name-only FETCH_HEAD config | grep '/values.yaml$'); do
      git -C /work/scratch/$MONO diff --quiet FETCH_HEAD:$f origin/main:$APP/$f || echo "$APP/$f: pins differ from $OLD"
    done
  } | tee -a $REC/pins-wait-$APP.log
}
# 1: the app's directory on the monorepo's main, and AaC/<Monorepo>'s last green build
has $MONO $APP
curl -s $J "$JENKINS_URL/job/AaC/job/$MONO/lastSuccessfulBuild/api/json?tree=number,artifacts%5BrelativePath%5D" | jq -c
# 2: deploy-pins.json entries that still name the old repo; once step 2 pushed, its wait, until
# any stage's state leaves its old key: from step 3's rename a copy to $OLD syncs an empty state
git -C /work/DockerImages fetch -q origin && git -C /work/DockerImages grep -l "\"pvginkel/$OLD\"" origin/main -- '*/deploy-pins.json'
[ -f $REC/pins-pushed-$APP.txt ] && { gh api repos/pvginkel/TerraformState/contents/argocd/$MONO/$APP --silent 2>/dev/null \
  && echo "pin wait: closed, a state is under argocd/$MONO/$APP" || pinwait; }
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
raw Architecture pipeline-producers.yaml | yq -c --arg id $PROD '.producers[] | select(.id == $id)'
# 9: the producer's old job (200 while it exists, 404 once deleted) and the old repo
curl -s -o /dev/null -w "AaC/$JOB: %{http_code}\n" $J "$JENKINS_URL/job/AaC/job/$JOB/api/json"
gh api repos/pvginkel/$OLD --jq '"archived: \(.archived)"'
```

| Step | Done when |
| --- | --- |
| 1. The directory | `$MONO $APP: present` |
| 2. The pins | no `deploy-pins.json` names `pvginkel/$OLD`. The wait, once step 2 pushed, is a read: run while no stage's state has left its old key (then the read prints `pin wait: closed`), and it holds at `pin push: built by #<n>` and `$OLD main: in $MONO`, with no `pins differ` line |
| 3. The state rename | the new key `present`, the old `absent`, no lock on either |
| 4. The plan | a read: run while step 5 is not done |
| 5. The registry switch | the entry names `$MONO` and `path: $APP` |
| 6. The diff | a read: run while the entry carries `autoSync: false` |
| 7. The sync | the entry has no `autoSync`; the Application reads `$MONO`, `autoSync: true`, `Synced` and `Healthy`; its hook's last run read `argocd/$MONO/$APP/$STAGE/terraform.tfstate` |
| 8. The producer | each producer's entry names `pvginkel/$MONO`, `AaC/$MONO` and `path: $APP` |
| 9. The old job and repo | `AaC/$JOB: 404` for each producer, and `archived: true` |
| 10. The annotation proof | the pilots only: their slice's record says |

Before step 7, the hook line can wait about 20 seconds and print `error: timed out waiting for
the condition`. That is a hook Job whose pod is gone, as on an app whose hook last ran days ago,
not a fault: step 7's sync runs the hook afresh, and the line then reads its state key.

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
  deleted: the monorepo's root files serve every app. A line of `$APP/.gitignore` that the root's
  lacks joins the root's, so a test run leaves nothing untracked: an anchored line re-anchored
  under the app directories (`/.cache/` becomes `/*/.cache/`), an unanchored one as it is
  (`__pycache__/`, which matches at any depth).
- `.kubecoder/project.yaml`: a project `$APP`. A project's key is its folder and its statements'
  working directory, so the old repo's `lint` and `test` statements carry over with `hook.repo`
  naming `$MONO` and `hook.path=$APP` beside it. A test that sets the hook parameters itself
  (kyverno's `tests/render-chart.py`) takes the same two.
- `$APP/README.md` describes the directory, not `$OLD`: its title the app's key, the
  Application's sources under `$APP/`, the state key `argocd/$MONO/$APP/$STAGE/terraform.tfstate`,
  no webhook of its own, and the producer published by `AaC/$MONO`. The comments that call `$OLD`
  "this repository" (`terraform/webhook.tf`, `terraform/providers.tf` and its `manage_webhook`
  description, the tfvars) say the same.

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
`$APP/config/dev/values.yaml`). Commit, then push and record the pushed commit, which the wait
reads:

```sh
git -C /work/DockerImages push origin HEAD:main && git -C /work/DockerImages rev-parse HEAD > $REC/pins-pushed-$APP.txt
```

The push rebuilds the app's images: DockerImages builds every image whose folder a push changed
(`utils.hasChanges("${image}/.*")`), `deploy-pins.json` included, and pins the new tag at every
entry of that image, in `$APP/` and in any other repo an entry names, which rolls as it does on any
build (jenkins' `mcp-filter` also pins trello-mcp). From here every build of the app's images pins
into `$APP/`, the push's own build first. An app with no entry pushes nothing and skips the wait;
neither pilot had one.

**The wait**, before step 3. Two builds leave pins the move must take in first. A DockerImages
build that started before the push can still commit a pin to `$OLD`: the job sets
`disableConcurrentBuilds()` without `abortPrevious`, under a 180-minute `timeout` (DockerImages
`Jenkinsfile`, `options`), so the push queues behind a running build instead of ending it, and
that build pins from its own checkout (its `Checkout` stage feeds `collectPins`), the entries as
they were before the push. The build that checks out the push pins the rebuilt tags into `$APP/`
while the app runs `$OLD`'s, which step 6's diff would show. Builds run one at a time, so once a
finished build has checked out the push, every build before it is done. Run the opening read's
`pinwait` until it prints `pin push: built by #<n>` and `$OLD main: in $MONO`, with no `pins
differ` line; its log, `$REC/pins-wait-$APP.log`, is the record that the wait ended before the
rename.

- `pin push: not built`: with `running: #<n>`, follow that build to its end, `track_build.py
  DockerImages --buildnr <n>`, which also waits for Argo CD to roll any pin it pushed; with no
  build running, the push's build is still queued. Then read again.
- `$OLD main: ahead of $MONO`: a commit reached `$OLD` after step 1's subtree took it, the pin of a
  build that started before the push. Once the opening read's Application line shows the old
  Application `Synced` at `$OLD`'s new head (its hook applied at the old key), bring the commit
  into the directory, in `/work/scratch/$MONO`:

  ```sh
  PATH=/tmp/gnubin:$PATH git subtree pull --prefix=$APP -m "$APP: $OLD's main since the subtree add" \
    https://github.com/pvginkel/$OLD.git main
  ```

  A line both sides pinned keeps the directory's tag, the later build's. Then step 1's prediction
  again, `/work/scratch/$OLD` fetched first so that `OLDREV` is that head, the push, and the read
  again.
- `$APP/<file>: pins differ from $OLD`, with `$OLD main: in $MONO`, before step 3: a build pinned
  rebuilt images into the directory, the push's own among them. The app takes them through `$OLD`,
  before the rename, as it takes any pin: each file the read names goes to `$OLD` as the directory
  has it, and the old Application syncs it at the old key. Once any stage's state has left its old
  key the read prints `pin wait: closed` instead and nothing goes to `$OLD`, keycloak's other stage
  included; step 6's diff shows a later rebuild's pins.

  ```sh
  cd /work/scratch/$OLD && git pull -q --ff-only
  git -C /work/scratch/$MONO show origin/main:$APP/config/$STAGE/values.yaml > config/$STAGE/values.yaml   # each file the read names
  git commit -qam "$APP: the image pins DockerImages wrote into $MONO $APP/" && git push origin HEAD:main
  track_build.py --deploy $OLD main $(git rev-parse HEAD)
  ```

  The app rolls onto the rebuilt images. The read then shows `$OLD main: ahead of $MONO`, the
  bullet above.

### 3. The state rename

```sh
cd /work/scratch/TerraformState && git pull --ff-only
gh api repos/pvginkel/TerraformState/git/matching-refs/heads/locks/argocd/$OLD/$STAGE/ --jq '.[].ref'
mkdir -p argocd/$MONO/$APP && git mv argocd/$OLD/$STAGE argocd/$MONO/$APP/$STAGE
git commit -m "argocd/$OLD/$STAGE → argocd/$MONO/$APP/$STAGE: $APP moves into $MONO"
git push origin main
```

An app with a volume (a `static-*-pv` module) first records each PersistentVolume, for step 7's
compare, all of them in one go, one line each. A module's PV is `<name>-pv`, `<name>` being the
module's `name`; the loop names every module's (jenkins-prd's `jenkins-pv build-cache-pv`). The
line's last fields name what backs the PV: `rootPath` a CephFS subvolume (`static-cephfs-pv`, whose
`handle` is the PV's own name), `local` a ZFS dataset (`static-zfs-pv`, a local PV), `handle` an RBD
image (`static-rbd-pv`):

```sh
for pv in <name>-pv …; do
  cexec iac kubectl get pv $pv -o json | jq -c '{pv: .metadata.name, uid: .metadata.uid,
    created: .metadata.creationTimestamp, claim: .spec.claimRef.name,
    rootPath: .spec.csi.volumeAttributes.rootPath, local: .spec.local.path,
    handle: .spec.csi.volumeHandle}'
done > $REC/pv-$NS-before.json
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

The destroy is the old repo's relay webhook, which `manage_webhook = false` lets go. A stage whose
old tfvars do not set `manage_webhook = true` has no webhook in its state, and its verdict holds no
change at all, rule (1)'s second expectation:

```text
presync: planning against argocd/<Monorepo>/<app>/<stage>/terraform.tfstate: nothing is applied
…
presync: the plan carries no changes
presync: plan only: nothing was applied and no volume was reattached
```

Either plan shows that the state is at the new key, where an empty state would plan a create of
every resource, and that its sops encryption does not bind the path. **Anything else is rule (1)'s
stop**: another change, the expected destroy missing, a plan that fails after reading the state.
Revert the rename ([before the registry switch](#before-the-registry-switch)) before anything else,
then stop. A plan that exits before its `presync: planning against` line (in the clone, `terraform
init` or a provider download) never read the state and is no verdict: keep its log under another
name and run the plan once more. Only if that one fails too is it rule (1)'s stop.

### 5. The registry switch

ArgoCDDeploy's `releases/values.yaml`, the app's entry: `repo:` names
`https://github.com/pvginkel/$MONO.git`, `path: $APP` (the render test refuses any other
directory), and each stage takes `autoSync: false`. Everything else in the entry stays as it is:
an upstream app keeps its `upstream:` block, each stage its `version`, and the entry its
`syncOptions`. `kc project lint` and `kc project test` green
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
reports `Resources: 0 added, 0 changed, 1 destroyed.`, or `Resources: 0 added, 0 changed, 0
destroyed.` for a stage whose old tfvars do not set `manage_webhook = true`; `$OLD`'s hooks no
longer list `https://deploy-hooks.webathome.org/api/webhook`, only its Jenkins one.

An app with a volume: the hook logs `presync: 0 volume(s) reattached`, and step 3's read run again
into `$REC/pv-$NS-after.json` is the same line for line, so the PV and what backs it are the
objects they were.

### 8. The producer

Run for each producer: `$APP-deploy` from `AaC/$OLD`, and a two-stage app's `$APP-dev-deploy`
from `AaC/$OLD-dev`. `AaC/$MONO` has a green build that archived
`$APP/docs/architecture/$PROD.yaml` (the read above): step 1's push started it. The producer proof
is that its artifact is, byte for byte, the one the old job published. The artifact carries the
app's image tags, and a `pins differ` copy to `$OLD` (step 2's wait) started `AaC/$JOB` again: run
the proof once that build is done, so that each side is its job's last green build.

```sh
curl -sf $J "$JENKINS_URL/job/AaC/job/$JOB/lastSuccessfulBuild/artifact/docs/architecture/$PROD.yaml" | sha256sum
curl -sf $J "$JENKINS_URL/job/AaC/job/$MONO/lastSuccessfulBuild/artifact/$APP/docs/architecture/$PROD.yaml" | sha256sum
```

Equal sums, or the producer proof stops the move. Then Architecture's `pipeline-producers.yaml`:
the producer's entry takes the monorepo shape ([argocd.md](argocd.md#an-app-in-a-monorepo)),
`repo: pvginkel/$MONO`, `jenkinsJob: AaC/$MONO`, `path: $APP`. Before the push, let
`AaC/Architecture` go idle, nothing building and nothing queued: a green build of any producer's
job queues one through its upstream trigger, and each new build aborts the one running
(`disableConcurrentBuilds(abortPrevious: true)` in Architecture's `Jenkinsfile`), which then ends
`NOT_BUILT`. Its last green build is `A`. Commit, push, and follow the build the push starts
(`track_build.py --hash <sha> AaC/Architecture`). It goes green, and the model is unchanged: the
producer's collected input is the same bytes in that build `B` as in `A`.

```sh
for b in $A $B; do
  curl -sf $J "$JENKINS_URL/job/AaC/job/Architecture/$b/artifact/producer-artifacts.tgz" \
    | tar -xzO --wildcards "producer-artifacts/$PROD/*$PROD.yaml" | sha256sum
done
```

### 9. The old job and repo

Each producer's old job only after that producer's step 8, its proof, its re-point and its
compare: until the re-point's green build, the collector copies the producer from `AaC/$JOB`, and
it copies from every registered producer's job, so a job an entry still names cannot go.

```sh
curl -sf $J "$JENKINS_URL/job/AaC/job/$JOB/config.xml" > $REC/AaC-$JOB-config.xml   # a rollback's copy
curl -sf -X POST $J "$JENKINS_URL/job/AaC/job/$JOB/doDelete"
```

Once every producer's old job is gone, the repo:

```sh
gh api -X PATCH repos/pvginkel/$OLD -F archived=true --jq .archived
```

The old repo's relay webhook went with step 7.

### 10. The annotation proof

Pilots only: slice 057 read it for both pilots in one wait after both moves, and it held (argo-cd
D69); a later move does not repeat it. A commit to the monorepo outside `$APP/` must leave the app
alone:

```sh
cexec iac kubectl get job -n argocd-hooks tf-presync-$NS -o jsonpath='{.metadata.creationTimestamp}{"\n"}'
cexec iac kubectl get configmap -n $NS tf-presync-revision -o jsonpath='{.data.revision}{"\n"}'
cexec iac kubectl get application -n argocd-prd $NS -o jsonpath='{.status.sync.status} {.status.reconciledAt}{"\n"}'
```

Read before the commit, after its webhook delivery, and after the periodic refresh that follows it
(`timeout.reconciliation: 30m`, no jitter): the app stays `Synced`, with the Job's creation time and
the ConfigMap's revision unchanged. The webhook refreshes no app whose directory the commit misses;
the periodic refresh comes at a 30-minute tick once the app's last reconcile is at least 30 minutes
old, so up to an hour after the push, and only then does the app's status revision reach the new
SHA. Never a hard refresh: it re-renders at HEAD and runs the app's hook (argo-cd D69).

## Why the order holds

- **The pins (2) before the rename (3), and nothing commits to `$OLD` until the switch (5).** With
  `selfHeal: false` the old Application syncs only on a new commit to `$OLD`
  ([argocd.md](argocd.md#registering-undeploying-and-unregistering-an-app)). A commit in that
  window, a DockerImages pin write or a hand edit, syncs it, and its hook applies against an empty
  state at the old key.
- **The wait (2) before the rename (3):** a DockerImages build that started before the pin push
  writes its pins from the entries it checked out, into `$OLD`, and the push queues behind it
  rather than ending it. Before the rename its pin syncs the old Application against the state;
  after it, against an empty one. The push's own build pins the app's rebuilt images into `$APP/`:
  before the rename they reach the app through `$OLD`; after it, step 6's diff shows them and
  rule (2) parks the move.
- **The directory pushed (1) before the plan (4):** the plan clones the monorepo at its SHA.
- **The rename (3) and a clean plan (4) before the switch (5):** the switched Application's first
  sync applies at the new key, which has to hold the state by then.
- **The switch with `autoSync: false` (5), the diff (6), then `autoSync` on (7):** the diff is read
  before anything applies.
- **A green `AaC/$MONO` build before the re-point (8):** a registered producer with no archived
  artifact fails the collector's discovery, and a failed collector run publishes nothing.
- **Each old job (9) after its own producer's re-point and green build:** until then the collector
  copies that producer from `AaC/$JOB`, and a collection that cannot copy a registered producer
  fails. keycloak's `AaC/KeycloakDeploy-dev` waits on `keycloak-dev-deploy`'s re-point as
  `AaC/KeycloakDeploy` waits on `keycloak-deploy`'s.

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

Step 7 done, 9 not: the app runs from the monorepo, `$OLD` is unarchived and each producer's
`AaC/$JOB` exists; one that step 9 already deleted comes back first, as [after
archival](#after-archival) does it. The move in reverse:

1. Each producer whose step 8 is done: its entry back to `repo: pvginkel/$OLD`,
   `jenkinsJob: AaC/$JOB`, no `path:`. Push; `AaC/Architecture` green.
2. The registry entry back to `$OLD` with `autoSync: false` on each stage. Push. The Application
   reads `$OLD`, and its diff (step 6's read) is the ConfigMap's `data.revision` alone, back to
   `$OLDREV`.
3. The state back to the old key, as [above](#before-the-registry-switch). Its plan from `$OLD`'s
   head carries one change, `github_repository_webhook.argocd[0]: create`: `$OLD`'s tfvars still
   set `manage_webhook = true`, and the move's sync destroyed the webhook. A stage whose tfvars do
   not set it plans no changes.
4. `autoSync` on. Push. Argo syncs `$OLDREV`, and the hook's apply at
   `argocd/$OLD/$STAGE/terraform.tfstate` recreates the webhook: `1 added, 0 changed, 0 destroyed`,
   or `0 added, 0 changed, 0 destroyed` for a stage whose tfvars do not set it.
5. The pins (2) back to `$OLD`, and `$APP/` and its project out of the monorepo in one commit.

### After archival

Step 9 done. Unarchive the repo, then recreate each producer's old job from its saved
`config.xml`, the push trigger with it; creating a job starts no build, so build it once:

```sh
gh api -X PATCH repos/pvginkel/$OLD -F archived=false --jq .archived
curl -sf -X POST $J -H 'Content-Type: application/xml' --data-binary @$REC/AaC-$JOB-config.xml \
  "$JENKINS_URL/job/AaC/createItem?name=$JOB"
curl -sf -X POST $J "$JENKINS_URL/job/AaC/job/$JOB/build"
```

Each build green, with `docs/architecture/$PROD.yaml` archived, then [after the
sync](#after-the-sync), whose step 1 needs them.
