# Jenkins' home from CephFS to RBD: the cutover

This cutover moves the Jenkins controller's home, the claim `jenkins-pvc` in `jenkins-prd`, from
its static CephFS volume `jenkins-pv` to a static RBD volume (ext4), `jenkins-home-pv`. That
takes the controller off the CephFS write path, where the 6.8 kernel leaks a memory stall that
wedges the node's PSI counter (`NodeMemoryStallCounterWedged`). The build cache,
`build-cache-pvc`, stays on CephFS: the build agents share it. In order, the cutover:

- has Terraform create the RBD image and its PV, while Jenkins still runs on CephFS;
- stops Jenkins once no build is running or queued, with Argo CD's auto-sync paused;
- hands the claim to the RBD volume and copies the home onto it, ownership, permissions and
  timestamps kept, and compares the two;
- starts Jenkins on RBD, then pushes the chart change and lets Argo CD sync again;
- checks the result.

Jenkins is expected to be down for about 25 minutes, from the stop in step 9 to Jenkins ready in
step 13, and 60 at the outside. The copy and its comparison take most of it. Jenkins' home holds
216k entries, 56,936 of them directories, and 8.1 GiB. A metadata walk of the CephFS home takes
about 25 s, and the home read at about 45 MB/s and 540 files/s in a sample. The copy is about
10 minutes, the comparison about 8, the stop, the swap and the clean-up about 3, and Jenkins'
start 2 to 5 (its startup probe allows 10).

The CephFS subvolume `jenkins-prd-home` and its PV `jenkins-pv` stay as they are: they are the way
back, and Jenkins' home has no backup on either volume. The copy mounts the subvolume read-only.
Nothing here deletes them; that is a later operator action.

Facts are as of 2026-11-08.

## Who does what

The cutover's work item runs every step, under the operator's go, except one. **The operator's
hands: step 4**, the push of the Terraform commit to `main`. Its PreSync hook applies that
commit's Terraform; a deploy repo has no hand `terraform apply` (AnsibleSpecs `decisions.md`,
"Operator-runs-TF rule"). The push is the keystroke that mutates the state, so the operator makes
it once step 3's plan has been read. No `ansible-playbook` runs.

A step that fails its check stops the cutover there. Take the way back for the point reached
([The way back](#the-way-back)) or ask; never carry on past a failed check.

## The commits

| Repo | Commit | What | Pushed in |
|---|---|---|---|
| HomelabAppsDeploy | `3c4ee95` (H1) | `jenkins/terraform/`: module `home_rbd`, the RBD image `jenkins-prd-jenkins-home` (40Gi) and the PV `jenkins-home-pv`, pre-bound to `jenkins-prd/jenkins-pvc`, `Retain`; the modules `home` and `build_cache` unchanged | step 4 (`main`) |
| HomelabAppsDeploy | `bea1eb4` (H2) | `jenkins/chart/`: `jenkins-pvc` through `ceph.rbd-pvc`, `ReadWriteOnce`, `volumeName: jenkins-home-pv` | step 14 (`main`) |
| PlatformAddOnsDeploy | `9a2bdbe` | `prometheus/`: the `NodeMemoryStallCounterWedged` rule's comment | step 17 |
| SOPs | `b113d83` | `sops/alert-fired.md`: the `NodeMemoryStallCounterWedged` row | step 17 |
| Ansible | this file's commit | this procedure | step 1 |

A rebase in step 1 changes the SHAs; H1 and H2 then name the rebased commits.

## Conventions

```sh
KC="--context prd"                                               # reads
KCW="--kubeconfig $HOME/.kube/config-prd-write --context prd"    # writes
NS=jenkins-prd
WORK=/work/scratch/jenkins-home-rbd                              # snapshots, manifests, logs
mkdir -p $WORK
```

The jenkins MCP takes the Jenkins snapshots. **Jobs snapshot**: `getJobs` with tree
`fullName,lastBuild[number,building],jobs[fullName,lastBuild[number,building],jobs[fullName,lastBuild[number,building]]]`,
every page (`limit` 10, `skip` 0, 10, 20, … until a page comes back empty), written to one file.
**Status snapshot**: `getStatus`.

Jenkins' crons run in UTC: `H 4` daily (`AaC` ha-fleet), 04:00 Sunday (`IaC/Scheduled Update`,
about 25 min), Wednesday (Calico) and Friday (certs), 05:30 daily (secret rotation) and `H 11`
daily (`IaC/Scheduled Drift`, 11:24, about 6 min). The `version-poller` CronJob runs at 05:00.
Jenkins does not catch up a trigger it missed while down, so steps 8 to 13 run outside
**04:00–06:30 and 11:00–11:45 UTC**.

## Before the window: Jenkins keeps running

### 1. The commits, current and pushed

```sh
cd /work/Ansible && git fetch -q && git status -sb | head -1     # this file's commit, ahead 1
git push origin main
for r in HomelabAppsDeploy PlatformAddOnsDeploy SOPs; do
  git -C /work/$r fetch -q && echo "$r: $(git -C /work/$r log --oneline origin/main..main | wc -l) ahead, $(git -C /work/$r log --oneline main..origin/main | wc -l) behind"
done
```

Expect 2, 1 and 1 ahead, and none behind. A repo that is behind is rebased (`git -C
/work/<repo> rebase origin/main`); for HomelabAppsDeploy, its gates then run again. Either way,
take H1 and H2 from its log and check what each carries:

```sh
cd /work/HomelabAppsDeploy
H1=$(git rev-parse HEAD~1) H2=$(git rev-parse HEAD)
git log --format='%h %s' -2 | cut -c1-90
git diff --stat origin/main $H1    # jenkins/terraform/main.tf, jenkins/terraform/modules/static-rbd-pv/main.tf
git diff --stat $H1 $H2            # jenkins/chart/templates/jenkins-pvc.yaml, jenkins/chart/values.yaml
kc project test --project jenkins,root     # after a rebase
```

Then push both to a topic branch, so they no longer live in this pod alone. No Jenkins job builds
HomelabAppsDeploy's other branches, and Argo CD reads `main` alone:

```sh
git -C /work/HomelabAppsDeploy push origin $H2:refs/heads/jenkins-home-rbd
```

The Ansible push runs `IaC/Build-Main`, `AaC/Ansible` and the SOPs site's `Publish`; wait for
them green (`track_build.py IaC/Build-Main --hash <sha>`).

### 2. The state before

```sh
cexec iac kubectl $KC get application -n argocd-prd jenkins-prd -o json \
  | jq '{policy: .spec.syncPolicy.automated, sync: .status.sync.status, health: .status.health.status}'
cexec iac kubectl $KC get pvc,pods -n $NS -o wide
cexec iac kubectl $KC get pv jenkins-pv build-cache-pv -o custom-columns=NAME:.metadata.name,DRIVER:.spec.csi.driver,STATUS:.status.phase,CLAIM:.spec.claimRef.name
```

Expect `automated` `{prune: true, selfHeal: false}`, `Synced`, `Healthy`; both PVCs `Bound` to
the CephFS PVs; the four Deployments' pods `Running`.

### 3. The plan of H1

The sheet in [argocd.md](argocd.md#planning-a-stages-terraform-as-its-hook-applies-it), with
`REPO=HomelabAppsDeploy APP_PATH=jenkins STAGE=prd NS=jenkins-prd BRANCH=jenkins-home-rbd
LOG=$WORK/plan-h1.log` and `REV=$H1` in place of the branch head. The log's change lines must be
exactly these two, and the exit code `0`:

```
presync: the plan carries 2 change(s):
presync:   module.home_rbd.homelab_rbd_image.this: create
presync:   module.home_rbd.kubernetes_persistent_volume_v1.this: create
```

Anything on `module.home` or `module.build_cache` stops the cutover.

### 4. H1 to main — the operator

The operator, once step 3's plan has been read:

```sh
git -C /work/HomelabAppsDeploy push origin $H1:main
```

Argo CD syncs `jenkins-prd`; its hook applies the two creates. `AaC/HomelabAppsDeploy` builds the
commit. The work item follows both and checks the result:

```sh
cexec iac kubectl $KC wait application/jenkins-prd -n argocd-prd --timeout=10m \
  --for=jsonpath='{.status.operationState.operation.sync.revision}'=$H1
cexec iac argocd app wait jenkins-prd --operation --sync --health --timeout 900
track_build.py AaC/HomelabAppsDeploy --hash $H1
cexec iac kubectl $KC logs -n argocd-hooks job/tf-presync-jenkins-prd | grep -E '^presync: |Creation complete|Apply complete|No changes'
cexec iac kubectl $KC get pv jenkins-home-pv -o json | jq '{phase: .status.phase, claim: .spec.claimRef, driver: .spec.csi.driver, handle: .spec.csi.volumeHandle, size: .spec.capacity.storage}'
cexec iac kubectl $KC get pods -n $NS -l app=jenkins
```

Expect Terraform's two `Creation complete` lines, `Apply complete! Resources: 2 added, 0 changed,
0 destroyed.` and the hook's `0 volume(s) reattached` (an apply prints Terraform's own lines, not
the plan Job's `presync:` change lines); `jenkins-home-pv` `Available`, its
claim `jenkins-prd/jenkins-pvc` with no uid, `rbd.csi.ceph.com`, `jenkins-prd-jenkins-home`,
`40Gi`; the Jenkins pod the one from step 2, not restarted.

### 5. The copy's source: a read-only claim on the CephFS home

A second PV over the same subvolume, made from `jenkins-pv`'s own CSI block, so the copy can read
the home while `jenkins-pvc` belongs to the RBD volume. CephFS allows the extra mount; the copy
mounts it read-only.

```sh
cexec iac kubectl $KC get pv jenkins-pv -o json | jq '{
  apiVersion: "v1", kind: "PersistentVolume",
  metadata: {name: "jenkins-home-cephfs-copy-pv"},
  spec: {
    accessModes: ["ReadWriteMany"], capacity: .spec.capacity, storageClassName: "",
    volumeMode: "Filesystem", persistentVolumeReclaimPolicy: "Retain",
    claimRef: {namespace: "jenkins-prd", name: "jenkins-home-cephfs-copy"},
    csi: (.spec.csi | .volumeHandle = "jenkins-home-cephfs-copy-pv")}}' > $WORK/copy-pv.json
jq .spec.csi.volumeAttributes.rootPath $WORK/copy-pv.json    # jenkins-pv's rootPath
cexec iac kubectl $KCW create -f $WORK/copy-pv.json
cexec iac kubectl $KCW create -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: jenkins-home-cephfs-copy
  namespace: jenkins-prd
spec:
  accessModes: [ReadWriteMany]
  resources: {requests: {storage: 1Mi}}
  storageClassName: ""
  volumeName: jenkins-home-cephfs-copy-pv
EOF
cexec iac kubectl $KC get pvc -n $NS jenkins-home-cephfs-copy
```

Expect `Bound`. Neither object carries Argo CD's tracking annotation, so Argo leaves them alone.

### 6. The manifests the window applies

```sh
IMAGE=$(cexec iac kubectl $KC get deploy jenkins -n $NS -o jsonpath='{.spec.template.spec.containers[0].image}')
echo $IMAGE      # jenkins/jenkins@sha256:…, the image Jenkins runs
cat > $WORK/claim-rbd.yaml <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: jenkins-pvc
  namespace: jenkins-prd
  annotations:
    argocd.argoproj.io/tracking-id: jenkins-prd:/PersistentVolumeClaim:jenkins-prd/jenkins-pvc
spec:
  accessModes: [ReadWriteOnce]
  resources: {requests: {storage: 1Mi}}
  storageClassName: ""
  volumeName: jenkins-home-pv
EOF
sed -e 's/ReadWriteOnce/ReadWriteMany/' -e 's/jenkins-home-pv/jenkins-pv/' $WORK/claim-rbd.yaml > $WORK/claim-cephfs.yaml
sed "s|@IMAGE@|$IMAGE|" > $WORK/copy-job.yaml <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: jenkins-home-copy
  namespace: jenkins-prd
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 5400
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsUser: 0
        runAsGroup: 0
      containers:
        - name: copy
          image: @IMAGE@
          command:
            - bash
            - -c
            - |
              set -euo pipefail
              extra=$(ls -A /dst | grep -vx 'lost+found' || true)
              if [ -n "$extra" ]; then echo "copy: /dst is not empty: $extra"; exit 1; fi
              t=$(date +%s)
              cp -a /src/. /dst/
              chown --reference=/src /dst
              chmod --reference=/src /dst
              touch --reference=/src /dst
              echo "copy: copied in $(( $(date +%s) - t )) s"
              t=$(date +%s)
              diff -r --no-dereference -x lost+found /src /dst
              echo "copy: contents identical, compared in $(( $(date +%s) - t )) s"
              meta() ( cd "$1" && find . -path ./lost+found -prune -o -printf '%y %m %U:%G %T@ %p\n' | LC_ALL=C sort )
              meta /src > /tmp/src.meta
              meta /dst > /tmp/dst.meta
              diff /tmp/src.meta /tmp/dst.meta
              echo "copy: $(wc -l < /tmp/src.meta) entries with the same type, mode, owner and mtime"
              echo "copy: $(find /dst/jobs -name config.xml | wc -l) job config.xml, $(find /dst/jobs -name build.xml | wc -l) build.xml, $(ls /dst/plugins | grep -cE '\.[jh]pi$') plugins"
              stat -c 'copy: root %U:%G %A' /dst
              df -hT /dst
          resources:
            requests: {cpu: 500m, memory: 512Mi}
          volumeMounts:
            - {name: src, mountPath: /src, readOnly: true}
            - {name: dst, mountPath: /dst}
      volumes:
        - name: src
          persistentVolumeClaim: {claimName: jenkins-home-cephfs-copy, readOnly: true}
        - name: dst
          persistentVolumeClaim: {claimName: jenkins-pvc}
EOF
cexec iac kubectl $KCW apply --dry-run=server -f $WORK/copy-job.yaml
```

`claim-rbd.yaml` is H2's render of the claim plus Argo CD's tracking annotation, so step 15's sync
adopts it unchanged; `claim-cephfs.yaml` is the way back's. The copy runs as root with no
`fsGroup`, so kubelet changes no ownership. `cp -a` keeps ownership, mode, timestamps and
symlinks (the home has 45 and no hard links); the three `--reference` lines give the volume's root
the CephFS root's `root:jenkins` and `drwxrwsr-x`, which Jenkins' `fsGroupChangePolicy:
OnRootMismatch` needs to skip its recursive walk. Then `diff -r` compares every file's content
and the metadata listing every entry's type, mode, owner and mtime, the root included. The copy
only reads CephFS: the leak is in the write path. A blank RBD image arrives formatted ext4 by
ceph-csi at its first mount, with `lost+found`.

### 7. The snapshot before

The jobs snapshot to `$WORK/jobs-before.json`, the status snapshot to `$WORK/status-before.json`.
Today's status lists four administrative monitors: Jenkins Update Notification, Check URI Encoding,
Recommended Java Version, Update Site Warnings.

## The window: Jenkins is down

Steps 8 to 13 run back to back, outside the cron hours.

### 8. Pause auto-sync

```sh
cexec iac kubectl $KCW patch application -n argocd-prd jenkins-prd --type json \
  -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
cexec iac kubectl $KC get application -n argocd-prd jenkins-prd -o jsonpath='{.spec.syncPolicy}{"\n"}'
```

Expect the retry block alone. Until step 15, no push to `jenkins/` and no refresh syncs
`jenkins-prd`, which would otherwise bring Jenkins up mid-copy. `releases` has no self-heal, so the
patch holds until `releases` next syncs: ArgoCDDeploy's next push to `releases/`, or a sync of
`releases` by hand, which on 2026-11-08 turned auto-sync back on four minutes before the stop.
Step 9 checks it held just before the stop, and step 13 after.

### 9. No build running or queued, then stop

The jenkins MCP's `getStatus`: `Full Queue Size` and `Buildable Queue Size` both `0`. A jobs
snapshot with no `"building": true` anywhere. No build's pod in the namespace, an agent's or a Job's:

```sh
cexec iac kubectl $KC get pods -n $NS -o json | jq -r '.items[]
  | select(.status.phase == "Running" or .status.phase == "Pending")
  | select([.metadata.ownerReferences[]?.kind] != ["ReplicaSet"]) | .metadata.name'
```

prints nothing, or only a `build-cache-cleanup` pod, the cache's CronJob. Anything running or queued: wait and check again; never stop over it. And
step 8's pause still holds:

```sh
cexec iac kubectl $KC get application -n argocd-prd jenkins-prd -o jsonpath='{.spec.syncPolicy.automated}{"\n"}'
```

prints an empty line; anything else, patch again (step 8) and check again. Once all four hold,
stop at once, within the minute:

```sh
cexec iac kubectl $KCW scale deploy/jenkins -n $NS --replicas=0
date -u +%FT%TZ > $WORK/down-at
cexec iac kubectl $KC wait pod -n $NS -l app=jenkins --for=delete --timeout=5m
```

Save that last jobs snapshot as `$WORK/jobs-at-stop.json`. A build that a trigger started in the
seconds between it and the stop shows in step 16's comparison.

### 10. The claim to the RBD volume

```sh
cexec iac kubectl $KCW delete pvc jenkins-pvc -n $NS --wait --timeout=2m
cexec iac kubectl $KC get pv jenkins-pv -o jsonpath='{.status.phase}{"\n"}'      # Released
cexec iac kubectl $KCW apply -f $WORK/claim-rbd.yaml
cexec iac kubectl $KC wait pvc/jenkins-pvc -n $NS --for=jsonpath='{.status.phase}'=Bound --timeout=2m
cexec iac kubectl $KC get pvc jenkins-pvc -n $NS -o jsonpath='{.spec.volumeName}{"\n"}'  # jenkins-home-pv
```

`jenkins-pv` is `Released` with `Retain`: its data is untouched.

### 11. The copy

```sh
cexec iac kubectl $KCW apply -f $WORK/copy-job.yaml
cexec iac kubectl $KC wait job/jenkins-home-copy -n $NS --for=jsonpath='{.status.conditions[-1:].type}' --timeout=95m
cexec iac kubectl $KC logs -n $NS job/jenkins-home-copy > $WORK/copy.log
cexec iac kubectl $KC get job jenkins-home-copy -n $NS -o jsonpath='{.status.succeeded}{"\n"}'
cat $WORK/copy.log
```

Expect `succeeded` `1`, no line from either `diff`, and these lines in order: `copy: copied in …`,
`copy: contents identical …`, `copy: 21…… entries with the same type, mode, owner and mtime` (the
source's own count, 213,781 at the cutover against the morning's 216k estimate), the counts, `copy: root root:jenkins drwxrwsr-x`, and `df` showing ext4 on `/dev/rbd…` with about
8–10 GiB used of 40 GiB. A `diff` line, a non-zero exit or a deadline stops the cutover: take
[the way back](#the-way-back) for step 10–12.

### 12. Clean up the copy

```sh
cexec iac kubectl $KCW delete job jenkins-home-copy -n $NS --cascade=foreground --wait
cexec iac kubectl $KCW delete pvc jenkins-home-cephfs-copy -n $NS --wait
cexec iac kubectl $KCW delete pv jenkins-home-cephfs-copy-pv --wait
cexec iac kubectl $KC get volumeattachments -o custom-columns=PV:.spec.source.persistentVolumeName,NODE:.spec.nodeName | grep -E 'jenkins-home|PV'
```

The PV is deleted under `Retain` and with `staticVolume`, so nothing touches the subvolume. Expect
no `jenkins-home-pv` attachment left once the copy's pod is gone (seconds).

### 13. Jenkins back up, on RBD

```sh
cexec iac kubectl $KCW scale deploy/jenkins -n $NS --replicas=1
cexec iac kubectl $KC rollout status deploy/jenkins -n $NS --timeout=15m
date -u +%FT%TZ > $WORK/up-at
cexec iac kubectl $KC get application -n argocd-prd jenkins-prd -o jsonpath='{.spec.syncPolicy.automated}{"\n"}'   # still empty
```

Jenkins is up once the rollout reports it; `up-at` minus `down-at` is the downtime.

## After the window: Jenkins runs

### 14. H2 to main

```sh
cd /work/HomelabAppsDeploy && git fetch -q
git log --oneline $H1..origin/main     # nothing, else rebase first (below)
git push origin $H2:main
```

Where `main` has moved past H1, rebase onto it (`git rebase origin/main`), take the new H2
(`git rev-parse HEAD`), check that `git log origin/main..HEAD` is that one commit and that no
commit since H1 touched `jenkins/` (`git log $H1..origin/main -- jenkins/` prints nothing),
re-run `kc project test --project jenkins,root`, and push it.

Jenkins is up, so `AaC/HomelabAppsDeploy` builds H2: `track_build.py AaC/HomelabAppsDeploy --hash
$H2`. Expect it green: it is the evidence's build. With auto-sync paused, `jenkins-prd` only goes
OutOfSync. Its diff is the read-only CLI's:

```sh
cexec iac kubectl $KCW annotate application -n argocd-prd jenkins-prd argocd.argoproj.io/refresh=normal --overwrite
cexec iac argocd app diff jenkins-prd
```

Expect the ConfigMap `tf-presync-revision`'s `data.revision` alone. A difference in the PVC or the
Deployment stops the cutover before step 15.

### 15. Auto-sync back on

```sh
cexec iac kubectl $KCW patch application -n argocd-prd jenkins-prd --type merge \
  -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":false}}}}'
cexec iac kubectl $KC wait application/jenkins-prd -n argocd-prd --timeout=10m \
  --for=jsonpath='{.status.operationState.operation.sync.revision}'=$H2
cexec iac argocd app wait jenkins-prd --operation --sync --health --timeout 900
cexec iac kubectl $KC logs -n argocd-hooks job/tf-presync-jenkins-prd | grep -E '^presync: |Creation complete|Apply complete|No changes'
cexec iac kubectl $KC get application -n argocd-prd jenkins-prd -o json \
  | jq '{policy: .spec.syncPolicy.automated, sync: .status.sync.status, rev: .status.sync.revision, health: .status.health.status}'
cexec iac kubectl $KC get application -n argocd-prd releases -o jsonpath='{.status.sync.status}{"\n"}'
```

Argo syncs H2 at once. Expect Terraform's `No changes.` and `Apply complete! Resources: 0 added, 0 changed, 0
destroyed.`, then the hook's `reattaching jenkins-pv`
and `1 volume(s) reattached`: the CephFS PV goes back to `Available`, still pre-bound to
`jenkins-pvc`, and stays so while the claim is bound to the RBD volume. The Application `Synced`
at H2, `Healthy`; `releases` `Synced`; the Jenkins pod the one from step 13, not restarted.

### 16. The evidence

1. **`jenkins-pvc` on RBD, `build-cache-pvc` on CephFS:**

   ```sh
   cexec iac kubectl $KC get pvc -n $NS -o custom-columns=PVC:.metadata.name,PV:.spec.volumeName,MODE:.spec.accessModes[0],STATUS:.status.phase
   cexec iac kubectl $KC get pv jenkins-home-pv build-cache-pv jenkins-pv -o custom-columns=PV:.metadata.name,DRIVER:.spec.csi.driver,STATUS:.status.phase,CLAIM:.spec.claimRef.name
   ```

   `jenkins-pvc` → `jenkins-home-pv`, `ReadWriteOnce`, `rbd.csi.ceph.com`; `build-cache-pvc` →
   `build-cache-pv`, `cephfs.csi.ceph.com`, both `Bound`; `jenkins-pv` `Available`.

2. **Jobs, build history, credentials and plugins as they were:**
   - A jobs snapshot to `$WORK/jobs-after.json`, compared with `jobs-at-stop.json`: the same jobs,
     each `lastBuild` the same number, except a job that has built since step 13
     (`AaC/HomelabAppsDeploy`'s H2 build). A job missing or a number lower stops here. A build
     numbered between the two snapshots that nothing in this cutover started is one a trigger
     started at the stop: read its result, and re-run it if it was aborted.
   - A status snapshot: the four administrative monitors of step 7, none added (a plugin that
     fails to load adds one), queue `0`.
   - Jenkins' log: no plugin, job or secret failure.

     ```sh
     cexec iac kubectl $KC logs -n $NS deploy/jenkins -c jenkins | grep -E 'SEVERE|Failed Loading|Failed to load|decrypt' | head -20
     ```

   - Credentials: the jenkins MCP answers through its Jenkins API token, which lives in the home,
     and step 14's build checks out and publishes with Jenkins' stored credentials.
3. **A build runs green after the cutover:** step 14's `AaC/HomelabAppsDeploy` build of H2.

### 17. The rest of the pushes, and the clean-up

Each push takes exactly its commit: check `git log origin/main..main` first.

```sh
git -C /work/PlatformAddOnsDeploy push origin main   # 9a2bdbe: prometheus-prd syncs; its render
                                                     # changes only tf-presync-revision
git -C /work/SOPs push origin main                   # b113d83: AaC/SOPs and the site's Publish
git -C /work/HomelabAppsDeploy push origin --delete jenkins-home-rbd
```

`prometheus-prd` is followed the way step 4 follows `jenkins-prd`, with its own name and the pushed
SHA, except that it has three sources (the upstream chart, its values and its chart), so its
revisions are lists and `.sync.revision` stays empty:

```sh
P=$(git -C /work/PlatformAddOnsDeploy rev-parse main)
cexec iac kubectl $KC wait application/prometheus-prd -n argocd-prd --timeout=10m \
  --for=jsonpath='{.status.operationState.operation.sync.revisions[2]}'=$P
cexec iac kubectl $KC get application -n argocd-prd prometheus-prd -o json \
  | jq '{phase: .status.operationState.phase, revs: .status.sync.revisions, sync: .status.sync.status, health: .status.health.status}'
```

Expect `Succeeded`, the pushed SHA in the last two revisions, `Synced`, `Healthy`.
`track_build.py AaC/SOPs --hash <sha>` follows the SOPs build. (`track_build.py --deploy`
waits on every Application of a monorepo, and an app whose directory a commit misses moves only at
Argo's next periodic refresh.) Then report: the downtime (`down-at`, `up-at`), `copy.log`, the three pieces
of evidence, and that `jenkins-prd-home` and `jenkins-pv` stay for the deletion.

## The way back

| Point reached | The way back |
|---|---|
| Steps 1–7 (nothing stopped) | Delete the copy's source claim and PV if step 5 made them (step 12's last two deletes). H1 stays on `main` with its image and PV unused: do not revert it, since its revert's apply would destroy the image, which `prevent_destroy` refuses, failing every sync of `jenkins-prd`. |
| Steps 8–9 (auto-sync paused, Jenkins stopped, claim untouched) | `scale deploy/jenkins --replicas=1`, then step 15's patch. |
| Steps 10–12 (the claim on RBD, the copy run, failed or cleaned up) | The CephFS home is as it was at the stop. Delete the copy Job if there, then the RBD claim; reattach the CephFS PV; give the claim back; delete the copy's source claim and PV; start Jenkins; turn auto-sync back on. The sync renders H1, which is the CephFS claim. Commands below. |
| Steps 13–14 (Jenkins up on RBD, auto-sync still paused) | Stop Jenkins (`--replicas=0`, wait), then the row above. If H2 is on `main`, revert it (`git revert`, push) before turning auto-sync back on. What Jenkins wrote on RBD since step 13 is lost unless it is copied back first, the copy in reverse, into CephFS's 10Gi quota. |
| Step 15 on (auto-sync back) | Pause auto-sync (step 8), then the row above, H2's revert included. A revert pushed with auto-sync on fails its sync: a claim's `volumeName` is immutable. |

The claim back to CephFS:

```sh
cexec iac kubectl $KCW delete job jenkins-home-copy -n $NS --ignore-not-found --wait
cexec iac kubectl $KCW delete pvc jenkins-pvc -n $NS --wait
cexec iac kubectl $KCW patch pv jenkins-pv --type merge -p '{"spec":{"claimRef":{"uid":null,"resourceVersion":null}}}'
cexec iac kubectl $KCW apply -f $WORK/claim-cephfs.yaml
cexec iac kubectl $KC wait pvc/jenkins-pvc -n $NS --for=jsonpath='{.status.phase}'=Bound --timeout=2m
cexec iac kubectl $KC get pvc jenkins-pvc -n $NS -o jsonpath='{.spec.volumeName}{"\n"}'   # jenkins-pv
cexec iac kubectl $KCW delete pvc jenkins-home-cephfs-copy -n $NS --ignore-not-found --wait
cexec iac kubectl $KCW delete pv jenkins-home-cephfs-copy-pv --ignore-not-found --wait
cexec iac kubectl $KCW scale deploy/jenkins -n $NS --replicas=1
cexec iac kubectl $KC rollout status deploy/jenkins -n $NS --timeout=15m
```

The patch on `jenkins-pv` is the hook's own reattach. `jenkins-home-pv` goes `Released`, and the
next sync's hook reattaches it, pre-bound to a claim bound elsewhere, which leaves it unused.
