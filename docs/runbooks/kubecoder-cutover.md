# KubeCoder's cutover to Argo CD

Moves KubeCoder's two stages, the `kubecoder-dev` and `kubecoder-prd` namespaces on the prd
cluster, off HelmCharts' Jenkins deploy and onto Argo CD, which syncs them from `KubeCoderDeploy`.
Dev goes end to end first, then it sits, then prd follows. The operator executes every step. This
file gives the order, the exact commands, what each must show, and what stops the cutover.

Context: slice 012's [`plan.md`](../../../AnsibleSpecs/slices/012_kubecoder_argo_cutover/plan.md),
whose rulings are behind every step; the `argo-cd/` set:
[`design.md`](../../../AnsibleSpecs/argo-cd/design.md),
[`decisions.md`](../../../AnsibleSpecs/argo-cd/decisions.md) (cited as `Dn`) and
[`phases.md`](../../../AnsibleSpecs/argo-cd/phases.md) B.3–B.5. This file points into
[`argocd.md`](argocd.md) for the diff table, the pre-flight, reading Argo and the
architecture-producer steps. Facts are as of 2026-09-22.

## Conventions

- **The operator's keystroke.** That covers every `terraform` and `helm` command, every `kubectl`
  write, every Argo sync, every registry commit and every Jenkins job change. The session
  accompanying the operator prepares the exact command or commit, waits, and reads the full
  output. Every push, to KubeCoder, KubeCoderDeploy, HelmCharts or Architecture, happens only on
  the operator's confirmation.
- **Claude reads no OpenBao value.** Only the no-destroy plan needs OpenBao-held credentials. It
  loads them inside the operator's own command, and the webhook secret stays a placeholder
  there. The pre-flight lists the stage's Secrets, which ESO fills from OpenBao, but its `jq`
  filter drops their values before anything is written, so it is safe for the session to run.
- **KubeCoder's Jenkinsfiles (D1).** The accompanying session writes both edits to
  `/work/KubeCoder` in this environment, at the step that calls for them. Each is checked with
  Jenkins' linter and pushed on the operator's confirmation. The steps state the outcome each
  edit must have. The linter only parses a scripted Jenkinsfile, so the first build after the
  push is the check of its steps:

  ```sh
  cd /work/KubeCoder && curl -sS -u admin:$JENKINS_TOKEN -F 'jenkinsfile=<Jenkinsfile' https://jenkins.webathome.org/pipeline-model-converter/validate
  ```

  A clean parse answers *"did not contain the 'pipeline' step"*; a syntax error answers
  *"Errors encountered validating Jenkinsfile"*.
- **Shorthand.** Set it in each shell that runs these steps; a restarted session sets it again:

  ```sh
  KC="--kubeconfig $HOME/.kube/config-prd-write --context prd"
  TFB='http://127.0.0.1:6061/?type=git&repository=https%3A%2F%2Fgithub.com%2Fpvginkel%2FTerraformState&ref=main&state='
  tfinit() { u=$1; shift; cexec iac terraform init -input=false -reconfigure "$@" \
    -backend-config="address=$u" -backend-config="lock_address=$u" -backend-config="unlock_address=$u"; }
  tags() { curl -s "http://registry:5000/v2/kubecoder-$1/tags/list"; echo; }
  ```

  `KC` is argocd.md's. `TFB` addresses this pod's terraform-backend-git sidecar. That is the same
  store and encryption that HelmCharts' deploy CLI and the PreSync hook use
  (`pvginkel/TerraformState`@`main`, sops/age), and state commands through it need no provider
  credentials.
- **Stops.** Each step says what stops the cutover, the state a stop leaves the stage in, and the
  way back. The ways back are at the end ([Ways back](#ways-back)). **Never revert a stage's
  registry commit, and never set `deployed: false`, to undo a cutover.** The ApplicationSet would
  delete the Application, and its resources finalizer would then delete `kubecoder-<stage>` and
  everything in it, env pods and the PVC included (D24, D27).
- **A stage's first sync restarts every env pod in it**, including any session running there.
  This environment runs in `kubecoder-prd`, so prd's syncs end the session driving them. Those
  steps are written for the operator alone, each with a named point to resume from.

## Facts

| | dev | prd |
| --- | --- | --- |
| Application, namespace | `kubecoder-dev` | `kubecoder-prd` |
| KubeCoderDeploy branch Argo syncs (D34) | `main` | `prd`, created by the promote job's first run (P2) |
| Build-Main's pins, in KubeCoderDeploy | `config/dev/values.yaml`, `dev-<n>` | `config/prd/values.yaml`, `prd-<n>` |
| HelmCharts registry entry | `configs/prd/kubecoder/dev/release.yaml` | `configs/prd/kubecoder/prd/release.yaml` |
| State moved from (HelmCharts) | `helm-charts/prd/kubecoder/dev/infra.tfstate` | `helm-charts/prd/kubecoder/prd/infra.tfstate` |
| State moved to (the hook's `argocd/<repo>/<stage>/terraform.tfstate`) | `argocd/KubeCoderDeploy/dev/terraform.tfstate` | `argocd/KubeCoderDeploy/prd/terraform.tfstate` |
| ZFS dataset, PV | `zpool5/kubecoder-dev` 20G, `kubecoder-dev-zfs-pv` | `zpool5/kubecoder` 80G, `kubecoder-prd-zfs-pv` |
| `manage_webhook`: owns KubeCoderDeploy's GitHub webhook | `true` | `false` |
| Cluster-scoped objects in the render | `Namespace/kubecoder-dev`, `ClusterRole` and `ClusterRoleBinding` `kubecoder-dev-nodes` | the same for `kubecoder-prd` |

- **Jenkins.**
  - `KubeCoder/Build-Main` builds KubeCoder's `main` from `Jenkinsfile`. Its UI configuration
    carries a GitHub push trigger and *do not allow concurrent builds*, with abort previous.
  - `KubeCoder/Deploy-PRD` (`Jenkinsfile.deploy-prd`) retags `dev-<n>` of Build-Main's last
    successful build as `prd-<its own number>` and `prd-latest`, then deploys prd through
    `IaC/HelmCharts`.
  - `AaC/KubeCoderDeploy` builds KubeCoderDeploy's `prd` branch (`Jenkinsfile.architecture`).
  - The promote job, `KubeCoder/Promote-PRD` (`Jenkinsfile.promote`, D2), is created at P2.
- **Build-Main's eight images** are `kubecoder-controller`, `-worker`, `-vsix`, `-claude-shim`,
  `-bot`, `-mcp`, `-manual` and `-ingress`. All but `claude-shim` are pinned and promoted.
  - `images.{controller,bot,mcp,ingress,manual}` are tag suffixes and carry their leading colon
    (`":524"`).
  - `controllerConfig.images.{worker,vsix}` are whole references
    (`registry:5000/kubecoder-worker:524`).
- **Build-Main pushes `dev-<n>` and `dev-latest`** for all eight images, and keeps doing so
  through and after the cutover. dev pins `dev-<n>`, and the promote job retags `dev-<n>` as
  `prd-<n>` (ANS-99). Nothing uses the bare `<n>` family: `kubecoder-*:176 … 185` and `latest` are
  left over from the retired `KubeCoder/KubeCoder` job (2026-07-21).
- **The hook image** is `registry:5000/argocd-hook:10`, the `homelab-shared` 0.2.1 pin that
  KubeCoderDeploy depends on. Terraform is pinned to the same version in the hook image and the
  `iac` sidecar, v1.16.3 (ANS-98; AnsibleSpecs `decisions.md`, "Terraform version").
- **KubeCoderDeploy has one GitHub webhook**, and it stays: Jenkins'
  (`683107093`, `https://jenkins.webathome.org/github-webhook/`, `push`).
- **`IaC/HelmCharts` redeploys every Jenkins-owned kubecoder stage** on any change under
  `configs/prd/kubecoder/`, not per stage (`changed()`, HelmCharts' `Jenkinsfile:198-204`).

## The sequence

1. **B1–B3, before either stage.** KubeCoderDeploy is pushed and green, Argo's own sync is
   confirmed, and the hook's Terraform is checked.
2. **D1.** `Build-Main` is rewritten and run, so the pins name a build that exists.
3. **D2.** dev's registry commit, with `autoSync: false`.
4. **D3.** dev's state surgery.
5. **D4.** the no-destroy plan, and no webhook at the relay.
6. **D5–D7.** the chart replay check, the pre-flight and the diff review.
7. **D8.** dev's manual sync, at the operator's moment.
8. **D9.** dev's `autoSync: true`.
9. **D10.** the loop, exercised once on dev.
10. **Let it sit.**
11. **P1.** the chart replay check, so the first promotion carries any replay.
12. **P2.** the promote job is created and run: prd's images retagged, `prd` created, `release-1`
    written.
13. **P3.** KubeCoderDeploy's architecture producer goes green on `prd`, then is registered.
14. **P4.** prd's registry commit, with `autoSync: false`.
15. **P5–P6.** prd's state surgery and no-destroy plan.
16. **P7–P9.** the chart replay check, the pre-flight and the diff review.
17. **P10.** prd's manual sync, by the operator alone.
18. **P11.** prd's `autoSync: true`.
19. **P12.** `Deploy-PRD` is deleted.
20. **P13.** a promotion, a rollback and a roll-forward.
21. **X1–X3, afterwards and unhurried.** the Helm release Secrets, HelmCharts' `_shared/` and
    `charts/kubecoder/`, and the KubeCoder task.
22. **[Exit](#exit).**

The order rests on these constraints:

- `Build-Main` is rewritten before dev's registry commit. The committed pins are hand-set to build
  523, which the `dev-` family's cap reaps as builds land, so the rewritten build has to run first
  for the first dev sync to name a build that exists.
- Per stage, the registry commit comes before the state surgery. Once a stage is flipped,
  HelmCharts' deploy CLI refuses it, so no Jenkins deploy can run Terraform against a half-moved
  state. The flip itself runs no hook; only the manual sync does.
- `Deploy-PRD` goes only after the promote job has retagged. Until then it promotes prd from
  `dev-<n>`, the same tag dev pins and the promote job retags.
- `configs/prd/kubecoder/_shared/` goes only after prd's state surgery. [WB-2](#wb-2-hand-a-stage-back-to-jenkins)
  needs it until then.
- prd's registry commit waits on this chain: `prd` created (P2), then the producer green on it,
  then the producer registered (P3). The flip drops KubeCoder's prd from HelmCharts' architecture
  artifact.
- The chart replay check runs again just before each diff review. At prd, a replay reaches the
  Application only through a promotion.

### Stopping points

A sitting is planned around these. For scale, Build-Main takes about twelve minutes and
`IaC/HelmCharts` about one: the dev half is about two hours in one sitting, the prd half three to
three and a half in one afternoon, and X1–X3 half an hour whenever.

**Stopping is safe after** these steps, because the stage keeps running as last deployed and
nothing acts on it meanwhile:

- D1. Build-Main deploys nothing, and its pin commits are inert while no Application exists. Dev
  is frozen at its last Jenkins deploy.
- D2, D3, D4 and D5–D7. Dev is flipped and refused by HelmCharts, the state is moved, nothing is
  applied. The Application sits OutOfSync until it is synced.
- D8's checks, D9 and D10. From D9, dev is in its target state.
- P2. `prd` exists and `Deploy-PRD` still works.
- P4 through P9, **but prd is frozen there**: no Jenkins deploy and no Argo sync reach it until
  P10. Stop there only if a prd hotfix can wait; otherwise the way out is
  [WB-2](#wb-2-hand-a-stage-back-to-jenkins).
- P11 and P12. Promotion is the only path from here.

**Do not stop inside** these:

- The state surgery, steps 2–6: from `state rm module.namespace` to the second push and
  `rm -rf $W/moves`. About ten minutes.
- A sync and its checks (D8, P10). Read the result before leaving.
- P3's merge to P4's push. The architecture collector is red in between.
- P13's three syncs. One window, with no Build-Main landing between them. About an hour.
- P2's first run if it goes red after `prd` moved: write `release-<m>` by hand first, or the
  rerun refuses.

**Before stepping away**, every time: the last step's output is read and its checks passed (a red
Build-Main means no KubeCoder build lands until it is fixed); no plaintext state is left on the
home volume (`$W/moves` after a surgery, `$W` after D8 and P10); and nothing is half-pushed in
`/work/KubeCoder`, `/work/HelmCharts` or `/work/KubeCoderDeploy`.

## Per-stage procedures

The dev and prd steps call these. First set the stage:

```sh
STAGE=dev   # or prd
W=$HOME/kubecoder-cutover/$STAGE
SRC="${TFB}helm-charts%2Fprd%2Fkubecoder%2F$STAGE%2Finfra.tfstate"
DST="${TFB}argocd%2FKubeCoderDeploy%2F$STAGE%2Fterraform.tfstate"
```

`$W` is on the home volume, which the `iac` sidecar shares. The state copies under it are
plaintext.

### The state surgery

R1–R3 and D32. The target key is the hook's own, derived from the clone URL's basename
(`/work/ArgoCDTools/argocd-hook/presync/backend.py:52-54`).

1. Make two working dirs, one per state, each holding nothing but an empty http backend. Then
   list HelmCharts' state:

   ```sh
   mkdir -p $W/src $W/dst $W/moves && printf 'terraform {\n  backend "http" {}\n}\n' | tee $W/src/backend.tf > $W/dst/backend.tf
   cd $W/src && tfinit "$SRC" && cexec iac terraform state list
   ```

   It must list exactly `module.namespace.kubernetes_namespace_v1.this`,
   `module.zfs.homelab_zfs_dataset.this` and `module.zfs.kubernetes_persistent_volume_v1.this`.
   Anything else stops the cutover, with nothing changed.
2. Hand the namespace to Argo (R2). HelmCharts' state stops tracking it, and the chart's tracked
   Namespace takes it at the first sync. The two tools must never both believe they own it.

   ```sh
   cd $W/src && cexec iac terraform state rm module.namespace
   ```

   It must print `Successfully removed 1 resource instance(s).`
3. Pull both states:

   ```sh
   cd $W/src && cexec iac terraform state pull > $W/moves/src.tfstate
   cd $W/dst && tfinit "$DST" && cexec iac terraform state pull > $W/moves/dst-before.tfstate
   python3 -c "import json,sys; s=json.load(open(sys.argv[1])); print(s['serial'], len(s['resources']))" $W/moves/dst-before.tfstate
   ```

   The destination must be empty, printing `0 0`: the key has never been written. Anything else
   stops, with only step 2 done: something has already applied to the key. Explain what did it
   before going on. Way back: [WB-1](#wb-1-fix-forward), or [WB-2](#wb-2-hand-a-stage-back-to-jenkins),
   whose re-import restores step 2.
4. Move the two addresses to their rebuilt names (R3), locally. `module.namespace` has no
   counterpart. The move writes to a file that does not exist yet, so the new state gets a lineage
   of its own; the pulled empty state carries `lineage: ""`.

   ```sh
   cd $W/moves && cexec iac terraform state mv -state=src.tfstate -state-out=dst.tfstate module.zfs.homelab_zfs_dataset.this homelab_zfs_dataset.env_storage && cexec iac terraform state mv -state=src.tfstate -state-out=dst.tfstate module.zfs.kubernetes_persistent_volume_v1.this kubernetes_persistent_volume_v1.env_storage
   cexec iac terraform state list -state=dst.tfstate; echo ---; cexec iac terraform state list -state=src.tfstate
   ```

   It must list `homelab_zfs_dataset.env_storage` and
   `kubernetes_persistent_volume_v1.env_storage`, and nothing after `---`. `-state` and
   `-state-out` work on local files only, and `moved` blocks cannot cross states, which is why the
   move goes pull, then local move, then push.
5. Push the **destination first** and the **source second**. An interruption between the two
   leaves the storage tracked twice, which the second push fixes; it never leaves it untracked.

   ```sh
   cd $W/dst && cexec iac terraform state push ../moves/dst.tfstate && cexec iac terraform state list
   cd $W/src && cexec iac terraform state push ../moves/src.tfstate && cexec iac terraform state list
   ```

   The first list shows the two `env_storage` addresses; the second shows nothing. A push refused
   for its serial or lineage stops: someone wrote the state after step 3. Pull again and repeat
   from step 3. Never use `-force`.
6. Delete the plaintext copies, `.backup` files included:

   ```sh
   rm -rf $W/moves
   ```

After this, HelmCharts' key for the stage is empty and KubeCoderDeploy's key holds the dataset and
the PV. Nothing in the cluster, on the pool or on GitHub has changed. Way back:
[WB-2](#wb-2-hand-a-stage-back-to-jenkins).

### The no-destroy plan

R4. This is the only look before the hook runs `terraform apply -auto-approve` for real; the hook
has no plan step of its own. It is one command, typed by the operator. The subshell keeps
anything it exports from outliving it.

- `bao-login.sh` and HelmCharts' `setup-env.sh prd` export the OpenBao-held `HOMELAB_*`
  credentials and `KUBE_CONFIG_PATH`: the prd-write kubeconfig, verified against srvk8s1.
- The rest is what the hook's environment gives KubeCoderDeploy's Terraform:
  - the stage's namespace;
  - the `zfs_pools` literal the hook receives (`/work/ArgoCDDeploy/config/prd/values.yaml:269`);
  - a token for the `github` provider, which is this environment's `GH_TOKEN`;
  - a placeholder webhook secret.
- `TF_DATA_DIR` keeps the backend-initialised `.terraform/` out of KubeCoderDeploy's working
  tree. `-upgrade` resolves the providers as the hook's clone does: that clone has no lock file.
- `cexec` layers this shell's environment over the sidecar's.

The working tree must be `origin/main`: run `git -C /work/KubeCoderDeploy pull --ff-only` first.
Then:

```sh
cd /work/Ansible && ( . scripts/bao-login.sh && cd /work/HelmCharts && . scripts/setup-env.sh prd && cd /work/KubeCoderDeploy/terraform && export TF_DATA_DIR=$W/plan TF_VAR_namespace=kubecoder-$STAGE TF_VAR_zfs_pools='{"zpool2":"srvk8s1","zpool3":"srvk8s2","zpool4":"srvk8s3","zpool5":"srvk8s4"}' TF_VAR_github_webhook_secret=plan-placeholder GITHUB_TOKEN="$GH_TOKEN" && tfinit "$DST" -upgrade && cexec iac terraform plan -input=false -var-file=../config/$STAGE/terraform.tfvars )
```

It must show:

- `setup-env: prd cluster — exported 5 credential(s), 0 missing.` and
  `setup-env: kube target … — verified prd`. A missing credential ends the command before the plan.
- The two refreshes: `homelab_zfs_dataset.env_storage: Refreshing state...` with the pool/dataset
  id, and `kubernetes_persistent_volume_v1.env_storage` with `kubecoder-<stage>-zfs-pv`.
- dev: `github_repository_webhook.argocd[0] will be created`, and
  `Plan: 1 to add, 0 to change, 0 to destroy.`
- prd: `No changes. Your infrastructure matches the configuration.`

HelmCharts' `static-zfs-pv` module and KubeCoderDeploy's `terraform/storage.tf` declare the same
dataset and PV attribute for attribute, so the storage plans no change. Anything else stops: above
all a destroy or a `must be replaced`, but an update in place or an error too. The stage is moved
and nothing is applied. Way back: [WB-1](#wb-1-fix-forward), a fix in KubeCoderDeploy's
`terraform/` followed by a new plan, or [WB-2](#wb-2-hand-a-stage-back-to-jenkins).

### The chart replay check

R5. KubeCoderDeploy copied `charts/kubecoder` and the stage values from HelmCharts, which kept
deploying KubeCoder. First fetch HelmCharts. Then run the replay command in KubeCoderDeploy's
`README.md` ("Copied from HelmCharts"), and its check of the root `architecture.yaml`. As of
2026-09-22 they start at these commits; the README's are authoritative:

```sh
git -C /work/HelmCharts fetch -q
git -C /work/HelmCharts log --oneline fb49c5c55cb5d782fec72d5176c20f2fd458d559..origin/main -- charts/kubecoder configs/prd/kubecoder homelab-root.crt
git -C /work/HelmCharts log --oneline 8cd9185e2a5246c48ba647e4284f2be5f668ff58..origin/main -- charts/kubecoder/architecture.yaml
```

- This runbook's own registry commits appear in the log. They add a `release.yaml`, flip
  `autoSync` or delete a stage `values.yaml`, and are not replayed: they have no counterpart in
  KubeCoderDeploy, and the copy point does not move for them.
- Replay anything else as the README says; every Argo-specific difference it lists stays.
  1. Move the README's copy point.
  2. Get the gate green.
  3. Commit, and push on confirmation.
  4. Get the change to the stage's Application. At dev it follows `main`, but before dev's first
     sync KubeCoderDeploy has no webhook to Argo, so refresh by hand (argocd.md "Webhooks"). At
     prd it arrives only through a promotion.

In every case the render gate must be green at the commit the stage will sync (R5):

```sh
cd /work/KubeCoderDeploy && git pull --ff-only && kc project test
```

### The pre-flight

This is the carried-in S11 ruling's item 2. Run argocd.md's pre-flight, in
[What a cutover does not change](argocd.md#what-a-cutover-does-not-change), from
`/work/KubeCoderDeploy` after `cexec iac tests/build-deps.sh`, with these values:

- `<ns>`: `kubecoder-$STAGE`
- `<stage>`: `$STAGE`
- `<repo>`: `https://github.com/pvginkel/KubeCoderDeploy.git`
- `<sha>`: the commit the stage syncs, `origin/main` for dev and `origin/prd` for prd.

Fetch the three cluster-scoped objects beside `live.json`, and pass all three to `stuck_fields.py`
after it:

```sh
cexec iac kubectl $KC get namespace kubecoder-$STAGE -o json --show-managed-fields > /tmp/ns.json
cexec iac kubectl $KC get clusterrole kubecoder-$STAGE-nodes -o json --show-managed-fields > /tmp/clusterrole.json
cexec iac kubectl $KC get clusterrolebinding kubecoder-$STAGE-nodes -o json --show-managed-fields > /tmp/clusterrolebinding.json
```

`live.json` carries the stage's Secrets without their values, since argocd.md's `jq` filter strips
them. Once the output is read, delete the files anyway:
`rm /tmp/render.yaml /tmp/live.json /tmp/ns.json /tmp/clusterrole.json /tmp/clusterrolebinding.json`.

The expected output is the
[findings' inventory](../../../AnsibleSpecs/handovers/argo-adoption-blind-spot/findings-2026-09-20.md)
without its `imagePullPolicy` row, which the chart now declares:

- **A.** Only the eight ESO-materialised Secrets, each produced by an `ExternalSecret` the render
  carries: `kubecoder-bot-token`, `kubecoder-client-token-bot`, `kubecoder-client-token-mcp`,
  `kubecoder-github-token`, `kubecoder-mcp-auth-token`, `kubecoder-samba-credential`,
  `kubecoder-secret-catalog` and `kubecoder-step-ca-provisioner-password`.
- **B.** `metadata.labels`, `metadata.annotations` and `spec.template.metadata.annotations`, and no
  `imagePullPolicy`. The `spec.template` row is the stale deploy timestamp on `kubecoder-bot` and
  `kubecoder-mcp`. Two more rows, `metadata.annotations.meta.helm.sh/release-name` and
  `…/release-namespace`, come from the ClusterRole and ClusterRoleBinding. They are the
  inventory's "+2" and are expected. dev printed 22, 21, 2, 1 and 1: `TOTAL 47`, the inventory's
  52 less its five `imagePullPolicy` rows (2026-09-23). prd printed 23, 21, 2, 2 and 2: `TOTAL 50`,
  the inventory's 55 less the same five. The extra label and release-name/-namespace pair are
  `Service/kubecoder-mcp-public`'s, and its annotations are otherwise rendered (2026-09-23).

This slice accepts those rather than fixing them: the Helm labels and annotations are inert, and
the stale timestamp rolls nothing. Any other object in A, or any other field in B, stops the
cutover. The stage is flipped and moved, not synced. Way back: [WB-1](#wb-1-fix-forward).
Declare the field in KubeCoderDeploy's chart, or the operator accepts it.

### The diff review

R8. Read the Application's diff in the UI: *App Diff* with *Compact diff*, or each resource's
*Diff* tab. To list the objects that differ:

```sh
cexec iac kubectl $KC get application -n argocd-prd kubecoder-$STAGE -o jsonpath='{range .status.resources[?(@.status=="OutOfSync")]}{.kind}/{.name}{"\n"}{end}'
```

The expected diff is the table closing argocd.md's
[Previewing a migrating app's diff before its cutover](argocd.md#previewing-a-migrating-apps-diff-before-its-cutover).
It is derived for dev. For prd, read `kubecoder-prd` for `kubecoder-dev` and `prd-latest` for
`dev-latest`, and add `Service/kubecoder-mcp-public`: only prd's render carries it, and it gains
the tracking-id alone.

- The five pinned containers' `imagePullPolicy` going from `Always` to `IfNotPresent` is expected.
- Every rendered object is listed, because each one gains the tracking-id. The hook Job is not
  listed.

**Anything else stops the cutover.** The stage is flipped and moved, not synced. Way back:
[WB-1](#wb-1-fix-forward) or [WB-2](#wb-2-hand-a-stage-back-to-jenkins).

### The sync checks

R9. After a stage's sync:

```sh
cexec iac kubectl $KC get application -n argocd-prd kubecoder-$STAGE -o jsonpath='{.status.sync.status} {.status.health.status} {.status.operationState.phase} {.status.operationState.syncResult.revision}{"\n"}'
cexec iac kubectl $KC get jobs -n argocd-hooks
cexec iac kubectl $KC logs -n argocd-hooks job/<the tf-presync-… Job listed> | grep -E 'Apply complete|Error'
cexec iac kubectl $KC get deploy,pods -n kubecoder-$STAGE
cexec iac kubectl $KC get deploy -n kubecoder-$STAGE kubecoder-controller -o jsonpath='{range .spec.template.spec.containers[*]}{.name} {.image} {.imagePullPolicy}{"\n"}{end}'
cexec iac kubectl $KC get configmap -n kubecoder-$STAGE kubecoder-controller-config -o jsonpath='{.data.controller\.yaml}' | grep -E 'kubecoder-(worker|vsix):'
cexec iac kubectl $KC get pods -n kubecoder-$STAGE -l homelab.local/env-id
```

They must show:

- The Application: `Synced Healthy Succeeded <the synced SHA>`.
- The hook Job `Complete`, its log ending `Apply complete! Resources: 1 added, 0 changed, 0
  destroyed.` on dev (the webhook) and `0 added` on prd.
- `kubecoder-controller`, `-bot` and `-mcp` at `1/1`, and the controller pod `4/4 Running`.
- The containers:
  - controller, ingress and manual at `registry:5000/kubecoder-<name>:<pin> IfNotPresent`;
  - `tunnel-reclaim` at `:latest Always`;
  - `worker` and `vsix` at the pinned build in the ConfigMap.
- Every env pod listed before the sync is `Running` again.

A failed hook applied nothing (argocd.md [Diagnosing a failed sync](argocd.md#diagnosing-a-failed-sync),
item 1). A failed sync phase applied part of the render, because it is not atomic (item 2).
Either way the stage is flipped and moved; the way back is [WB-1](#wb-1-fix-forward). The next
manual sync reruns the hook, whose apply is idempotent against the moved state. If a controller
does not come up, read its pod's events. An unpullable pin is the likely cause: `tags <name>` must
show the pinned tag.

## Before either stage

### B1: KubeCoderDeploy pushed and green

R5. Argo, Build-Main's pin writes and the promote job all clone `origin`, never `/work`.

```sh
cd /work/KubeCoderDeploy && git status -sb | head -1 && kc project test
```

Push anything `main` carries past `origin/main` on the operator's confirmation. Pushing is inert,
because no Application syncs from KubeCoderDeploy before a registry commit names it. A push starts
`AaC/KubeCoderDeploy`, which fails on the missing `prd` branch until P2 creates it; that red build
is expected. The status must read `## main...origin/main`, with nothing ahead, and the gate must
be green. A red gate stops the cutover, with nothing changed.

### B2: Argo's own sync is done

This is a carried-in ruling. KubeCoderDeploy's webhook Terraform reads the hook's
`TF_VAR_github_webhook_secret`, and the hook's ClusterRole must be the narrowed one:

```sh
cexec iac kubectl $KC get application -n argocd-prd argocd-prd -o jsonpath='{.status.sync.status} {.status.health.status}{"\n"}'
cexec iac kubectl $KC get secret -n argocd-hooks argocd-hook-credentials -o go-template='{{range $k, $v := .data}}{{$k}}{{"\n"}}{{end}}' | grep -x TF_VAR_github_webhook_secret
cexec iac kubectl $KC get clusterrole tf-presync -o jsonpath='{range .rules[*]}{.resources}{"\n"}{end}'
```

The go-template prints key names, never values. The output must be `Synced Healthy`, the key name,
and exactly `["persistentvolumes"]` and `["secrets"]`. All three held on 2026-09-22. Otherwise
the cutover stops, with nothing changed: sync Argo first, per argocd.md
[Upgrading Argo CD](argocd.md#upgrading-argo-cd) steps 2–4.

### B3: the hook's Terraform matches the sidecar's

The surgery's states are written by the sidecar's Terraform, and the hook reads them at its first
apply. Print both versions:

```sh
cexec iac terraform version | head -1
cd /work/KubeCoderDeploy && cexec iac helm template kubecoder-dev chart --values config/dev/values.yaml --set hook.repo=https://github.com/pvginkel/KubeCoderDeploy.git,hook.revision=0123456789abcdef0123456789abcdef01234567,hook.stage=dev,hook.namespace=kubecoder-dev | grep 'image: registry:5000/argocd-hook:'
cexec iac kubectl -n development run hook-tf-version --rm -i --restart=Never --image=registry:5000/argocd-hook:<tag from the render> --command -- terraform version
```

The render must name `argocd-hook:10`, and both must print the same version, v1.16.3: the pin
both images share (ANS-98). The `development` namespace is where the default kubeconfig may create
a throwaway pod.

A different version means one side was rebuilt without the other. The cutover stops here, with
nothing changed: bring the images back to one pin (AnsibleSpecs `decisions.md`, "Terraform
version"), move `homelab-shared`'s `hook.imageTag` and KubeCoderDeploy's dependency if the hook
was rebuilt, and run this check again.

## Dev

### D1: Build-Main rewritten and run

R14, D47, D1. The accompanying session edits `/work/KubeCoder/Jenkinsfile`. After the edit,
`Build-Main` does these four things:

1. **Declares** `properties([disableConcurrentBuilds(abortPrevious: true), pipelineTriggers([githubPush()])])`.
   These are the job's two properties from its UI configuration. A `properties` step replaces
   them, so a bare `disableConcurrentBuilds()` would drop the push trigger.
2. **Builds all eight images to the destinations it has today,**
   `registry:5000/kubecoder-<name>:dev-${currentBuild.number}` and
   `registry:5000/kubecoder-<name>:dev-latest`. They are unchanged: dev pins `dev-<n>`, the
   promote job retags it, and `Deploy-PRD` keeps promoting prd from it until P12.
3. **Then calls the pin writer once**, in the `k8s` container, which carries `git`. The call
   makes one commit on KubeCoderDeploy's `main`. `prd-<n>` is a forward reference, which the
   promote job satisfies.

   ```groovy
   String n = "${currentBuild.number}"
   cicd.writeVersionPins(repo: 'pvginkel/KubeCoderDeploy', message: "ci: image pins from KubeCoder/Build-Main #${n}", pins: [
       'config/dev/values.yaml': [
           'images.controller': ":dev-${n}", 'images.bot': ":dev-${n}", 'images.mcp': ":dev-${n}",
           'images.ingress': ":dev-${n}", 'images.manual': ":dev-${n}",
           'controllerConfig.images.worker': "registry:5000/kubecoder-worker:dev-${n}",
           'controllerConfig.images.vsix': "registry:5000/kubecoder-vsix:dev-${n}"
       ],
       'config/prd/values.yaml': [
           'images.controller': ":prd-${n}", 'images.bot': ":prd-${n}", 'images.mcp': ":prd-${n}",
           'images.ingress': ":prd-${n}", 'images.manual': ":prd-${n}",
           'controllerConfig.images.worker': "registry:5000/kubecoder-worker:prd-${n}",
           'controllerConfig.images.vsix': "registry:5000/kubecoder-vsix:prd-${n}"
       ]
   ])
   ```

4. **Deploys nothing.** The `Deploy Helm charts` stage is gone, and with it `cicd.helmDeploy()`.

Lint the file and push it on confirmation. The push starts Build-Main `#<n>`; Build-Main was at
#524 on 2026-09-22. From here Build-Main no longer deploys dev, which stays at its last Jenkins
deploy until D8. That is ruled acceptable: nothing depends on dev picking up builds meanwhile.

Check the build:

```sh
N=<the build number>
for i in controller worker vsix claude-shim bot mcp manual ingress; do echo "$i $(tags $i | grep -o "\"dev-$N\"")"; done
git -C /work/KubeCoderDeploy pull --ff-only && git -C /work/KubeCoderDeploy show --stat --format='%h %s' HEAD && git -C /work/KubeCoderDeploy show HEAD | grep '^[-+] '
cd /work/KubeCoderDeploy && kc project test
curl -sg -u admin:$JENKINS_TOKEN https://jenkins.webathome.org/job/KubeCoder/job/Build-Main/config.xml | grep -o 'GitHubPushTrigger\|DisableConcurrentBuildsJobProperty' | sort -u
```

It must show:

- every image carrying `"dev-<n>"`;
- one commit, `ci: image pins from KubeCoder/Build-Main #<n>`, touching exactly
  `config/dev/values.yaml` and `config/prd/values.yaml`, seven pins each, old value out and new in;
- the gate green;
- both property names.

Stops:

- **The build fails before the pin writer.** At most some images are pushed; there is no pin
  commit, and both stages are untouched. Way back: [WB-1](#wb-1-fix-forward).
- **The pin commit or the gate is wrong.** Nothing syncs from `main` yet. Revert that commit on
  KubeCoderDeploy's `main`, then take [WB-1](#wb-1-fix-forward).
- **A property name is missing.** The job has lost its push trigger or its lock; declare it in
  the Jenkinsfile.

### D2: dev's registry commit

R6 and R7. The entry has no `chart:` key (D38, slice 008's `resolve()`), and `deployed` and
`autoSync` are plain booleans. In HelmCharts, which is a shared working tree, stage by name. Write
the entry before removing `values.yaml`, since removing the stage's only file removes its
directory:

```sh
cd /work/HelmCharts && git pull --ff-only && cat > configs/prd/kubecoder/dev/release.yaml <<'EOF'
reconciler: argo-cd
deployed: true
autoSync: false
repo: https://github.com/pvginkel/KubeCoderDeploy.git
targetRevision: main
EOF
git rm -q configs/prd/kubecoder/dev/values.yaml && kc project test && git add configs/prd/kubecoder/dev/release.yaml && git commit -m "kubecoder dev: Argo CD reconciles the stage from KubeCoderDeploy's main (slice 012)"
```

`kc project test` gates the entry's shape. Push on confirmation. The push does three things:

- The ApplicationSet generates `kubecoder-dev` within seconds. It is OutOfSync, with no
  `syncPolicy` and no operation; the flip runs no hook.
- HelmCharts refuses the dev stage from here on.
- **`IaC/HelmCharts` redeploys `kubecoder-prd`**, which is still Jenkins'. That is R7:
  `changed()` matches `configs/prd/kubecoder/.*`, not a single stage. prd's release is upgraded in
  place, and **every prd env pod restarts**, this session's included. The chart stamps each deploy
  into `KUBECODER_DEPLOYMENT_ID`. The new controller's `upgrade_roll` then deletes and recomposes
  every env pod composed under an older id. The env's `/work` survives. Seen at D2 on 2026-09-23
  (`IaC/HelmCharts` #6670, `kubecoder-prd` revision 294). Push from a session you can lose, and
  resume from ANS-102.

Check the result:

```sh
cexec iac kubectl $KC get application -n argocd-prd kubecoder-dev -o jsonpath='{.status.sync.status} {.spec.source.repoURL} {.spec.source.targetRevision} [{.spec.syncPolicy}] [{.status.operationState.phase}]{"\n"}'
cexec iac kubectl $KC get jobs -n argocd-hooks
cd /work/HelmCharts && cexec iac poetry run deploy template prd/kubecoder --stage dev
```

It must show:

- `OutOfSync https://github.com/pvginkel/KubeCoderDeploy.git main [] []`;
- no `tf-presync-…` Job;
- `prd/kubecoder@dev is deployed by 'argo-cd', not jenkins — refusing to template.`

Wait for the `IaC/HelmCharts` build to finish green before D3.

If the Application does not appear, stop and read argocd.md
[Diagnosing a failed sync](argocd.md#diagnosing-a-failed-sync), item 4: the registry webhook, or
the applicationset-controller. The stage is flipped, HelmCharts refuses it, nothing has synced,
and dev keeps running as last deployed. Way back: [WB-1](#wb-1-fix-forward) or
[WB-2](#wb-2-hand-a-stage-back-to-jenkins).

### D3: dev's state surgery

R1–R3. Set `STAGE=dev` and its variables, then run [the state surgery](#the-state-surgery).

### D4: the no-destroy plan, and no webhook at the relay

R4. Run [the no-destroy plan](#the-no-destroy-plan). dev's must end
`Plan: 1 to add, 0 to change, 0 to destroy.`, the one add being `github_repository_webhook.argocd[0]`.

The dev sync creates KubeCoderDeploy's webhook to the relay. If a hook already points there, the
create fails on GitHub's hook-already-exists:

```sh
cexec iac gh api repos/pvginkel/KubeCoderDeploy/hooks --jq '.[] | [.id, .config.url] | @tsv'
```

It must list only `683107093	https://jenkins.webathome.org/github-webhook/`, which is Jenkins'
and stays. A hook at `https://deploy-hooks.webathome.org/api/webhook` stops the cutover, with the
stage flipped and moved, not synced. Delete that one in GitHub's UI (the repository's *Settings →
Webhooks*), then check again.

### D5–D7: replay check, pre-flight, diff review

1. [The chart replay check](#the-chart-replay-check). Here it lists D2's commit, which is not
   replayed.
2. [The pre-flight](#the-pre-flight), with `<sha>` the tip of `origin/main`.
3. [The diff review](#the-diff-review).

### D8: dev's manual sync

R9, at the operator's chosen moment. The sync rolls dev's controller, which is `Recreate` at one
replica, so there is a short control-plane outage. It also restarts every env pod in the stage,
and a session in `kubecoder-dev` ends with it. First record what should come back:

```sh
cexec iac kubectl $KC get pods -n kubecoder-dev -l homelab.local/env-id -o name
```

Then, in the UI (`https://argocd.home`): `kubecoder-dev`, *Sync*, keep the defaults, then
*Synchronize*. The Namespace applies in wave −1. Then the PreSync Job runs: it clones at the
synced SHA and applies against `argocd/KubeCoderDeploy/dev/terraform.tfstate`, creating the
webhook and leaving the dataset and PV unchanged. Then the rest applies.

Run [the sync checks](#the-sync-checks), then:

```sh
cexec iac gh api repos/pvginkel/KubeCoderDeploy/hooks --jq '.[] | [.id, .config.url] | @tsv'
cd $W/dst && cexec iac terraform state list
```

There must now be a second hook, at `https://deploy-hooks.webathome.org/api/webhook`, and the
state must list `github_repository_webhook.argocd[0]` beside the two `env_storage` addresses.
Then run `rm -rf $W`.

D41 left open whether the hook's classic PAT can create the webhook. If GitHub refuses the
create, the hook fails and nothing is applied. Give the hook's PAT (`eso/prd/argocd-hooks/git`)
the webhook scope GitHub's error names (argocd.md
[Rotating a token or secret](argocd.md#rotating-a-token-or-secret)), then sync again.

### D9: dev's `autoSync: true`

R10, once D8's checks pass:

```sh
cd /work/HelmCharts && git pull --ff-only && sed -i 's/^autoSync: false$/autoSync: true/' configs/prd/kubecoder/dev/release.yaml && git diff && kc project test && git add configs/prd/kubecoder/dev/release.yaml && git commit -m "kubecoder dev: Argo CD auto-syncs the stage (slice 012)"
```

Push on confirmation. `IaC/HelmCharts` redeploys `kubecoder-prd` again, as in R7, and every prd env
pod restarts again, as at D2. Within about ten seconds:

```sh
cexec iac kubectl $KC get application -n argocd-prd kubecoder-dev -o jsonpath='{.spec.syncPolicy.automated}{"\n"}'
```

It must print `{"prune":true,"selfHeal":false}`. No sync starts, because the Application is
Synced. From here every pin commit on `main` rolls dev. Every change to `worker` or `vsix`
restarts dev's env pods, and that includes the version poller's weekly `Build-Main` rebuilds.

If the policy does not appear, stop and look at the ApplicationSet (argocd.md). Way back:
`autoSync: false` removes the policy again. That is safe, unlike `deployed: false`.

### D10: the loop, once

R11. Start a build with *Build Now* on `KubeCoder/Build-Main`, or with a push to KubeCoder's
`main`. Each link must show:

1. **Image build.** Build-Main `#<n>` is green, and `tags <name>` shows `"dev-<n>"` for all eight
   images.
2. **Tags commit.**
   `git -C /work/KubeCoderDeploy fetch -q && git -C /work/KubeCoderDeploy log -1 --format='%H %s' origin/main`
   prints `ci: image pins from KubeCoder/Build-Main #<n>`.
3. **Webhook.** The relay logs `delivery <id> event=push: both receivers accepted`, and
   argocd-server logs `Received push event` for KubeCoderDeploy. The commands are in argocd.md
   [Webhooks](argocd.md#webhooks) and
   [Reading Argo without the CLI](argocd.md#reading-argo-without-the-cli).
4. **Auto-sync.** This must print the commit's SHA, `Succeeded` and `true`, and the controller
   must be at `:dev-<n>`:

   ```sh
   cexec iac kubectl $KC get application -n argocd-prd kubecoder-dev -o jsonpath='{.status.operationState.syncResult.revision} {.status.operationState.phase} {.status.operationState.operation.initiatedBy.automated}{"\n"}'
   ```

A missing link stops the cutover. Dev stays on the previous pin and keeps running, so no way back
is needed. Fix the link and push again. A missing delivery shows in GitHub's *Recent Deliveries*
on the relay hook.

## Let it sit

Dev now runs from Argo and prd from Jenkins. `Build-Main` pins dev, `Deploy-PRD` still promotes
prd from `dev-<n>`, and HelmCharts still deploys prd. Move on when the operator is satisfied that
dev behaves: some ordinary builds through the loop, without a surprise. Nothing sets the length.

## Prd

### P1: replay check before the first promotion

Run [the chart replay check](#the-chart-replay-check). The log now lists D2's and D9's commits,
which are not replayed. Anything replayed now lands on `main`, and dev auto-syncs it: check that
dev is `Synced Healthy` at that commit. P2's promotion then carries it to `prd`.

### P2: the promote job, first run

D2 and R14. Create the job in Jenkins:

1. *New Item* in the `KubeCoder` folder, named `Promote-PRD`, of type *Pipeline*.
2. *Pipeline script from SCM*, Git, repository `https://github.com/pvginkel/KubeCoderDeploy.git`,
   credentials `5f6fbd66-b41c-405f-b107-85ba6fd97f10`, branch `*/main`, script path
   `Jenkinsfile.promote`. Save.
3. *Build Now*. The first run registers the `commit` parameter and the concurrency lock, and
   promotes the tip of `main`.

The job runs D47's order, each step only after the one before it succeeded:

1. It retags each of the seven images the commit's `config/prd/values.yaml` pins, from `dev-<n>`
   to `prd-<n>`. An existing `prd-<n>` is left as it is.
2. It fast-forwards `prd` to the commit; the first run creates the branch.
3. It writes and pushes the annotated tag `release-1`.

Check the run:

```sh
git -C /work/KubeCoderDeploy fetch -q --tags origin && git -C /work/KubeCoderDeploy rev-parse origin/prd origin/main && git -C /work/KubeCoderDeploy show --no-patch release-1
N=<the n config/prd/values.yaml pins at that commit>
for i in controller worker vsix bot mcp manual ingress; do echo "$i $(tags $i | grep -o "\"prd-$N\"")"; done
```

It must show:

- `origin/prd` at the commit the build log names;
- `release-1`'s message naming that commit and the seven
  `registry:5000/kubecoder-<name>:prd-<n>` references;
- `"prd-<n>"` on all seven images.

From here `prd` exists, but nothing syncs it yet. `Deploy-PRD` is still live until P12; do not run
it after P4.

Each stop is a red build:

- **A refusal before the registry.** The commit is not on `main`, the job reports *nothing to
  promote*, the move is not a fast-forward, `release-<m>` already exists on origin, or a pin is
  not `registry:5000/kubecoder-<name>:prd-<n>`. Nothing changed; fix the cause and run again.
- **A failure before *Advancing prd*.** At most some new `prd-<n>` aliases exist. They are
  harmless; run again.
- **A failure at *Recording the release*, after `prd` moved.** A rerun refuses with *nothing to
  promote*, so write the tag by hand, on confirmation. `<m>` is the failed build's number and
  `<n>` the build `config/prd/values.yaml` pins at `<sha>`. The message is the job's: the first
  line, a blank line, then the seven references in the job's order.

  ```sh
  N=<n>
  cd /work/KubeCoderDeploy && git tag -a release-<m> -m "release-<m>: <sha> promoted to prd by KubeCoder/Promote-PRD #<m>" -m "$(for i in controller bot mcp ingress manual worker vsix; do echo registry:5000/kubecoder-$i:prd-$N; done)" <sha> && git push origin refs/tags/release-<m>
  ```

This run is the first check of the job's sandbox and its steps; the linter only parsed the file.

### P3: KubeCoderDeploy's architecture producer

This is slice 014's ordering. Follow argocd.md
[Giving an app its own architecture producer](argocd.md#giving-an-app-its-own-architecture-producer),
steps 2, 4 and 5, with `<Repo>` = `KubeCoderDeploy`, `<stage>` = `prd` and `<app>-deploy` =
`kubecoder-deploy`:

1. **Step 2**, the handover equality check. Run it with `/work/KubeCoderDeploy` checked out at
   `origin/prd`'s commit (`git -C /work/KubeCoderDeploy checkout -q origin/prd`). It must exit 0.
   Then return the checkout to `main` (`git -C /work/KubeCoderDeploy checkout -q main`): the
   no-destroy plan and the replay check pull `main`, and a detached checkout fails them.
2. **Step 4.** `AaC/KubeCoderDeploy` already exists. Build it if the push that created `prd` did
   not start it. Its first green build archives `docs/architecture/kubecoder-deploy.yaml`.
3. **Step 5.** Open the PR against `pipeline-producers.yaml` in `pvginkel/Architecture`; the
   operator merges it.

   ```yaml
   - id: kubecoder-deploy
     repo: pvginkel/KubeCoderDeploy
     jenkinsJob: AaC/KubeCoderDeploy
   ```

From registration until P4's flip, both producers declare KubeCoder's prd ids. The collector
fails on the duplicates and the Architecture job is red. That is expected, and P4's HelmCharts
architecture build clears it. Go on to P4 without a pause.

A red equality check or a red producer build stops the cutover. Nothing is registered, and prd
is still Jenkins'. Fix KubeCoderDeploy's `architecture.yaml`, then promote again, since the
producer builds `prd`.

### P4: prd's registry commit

R6:

```sh
cd /work/HelmCharts && git pull --ff-only && cat > configs/prd/kubecoder/prd/release.yaml <<'EOF'
reconciler: argo-cd
deployed: true
autoSync: false
repo: https://github.com/pvginkel/KubeCoderDeploy.git
targetRevision: prd
EOF
git rm -q configs/prd/kubecoder/prd/values.yaml && kc project test && git add configs/prd/kubecoder/prd/release.yaml && git commit -m "kubecoder prd: Argo CD reconciles the stage from KubeCoderDeploy's prd branch (slice 012)"
```

`_shared/` stays until X2. Push on confirmation. The push has these effects:

- `kubecoder-prd` is generated: OutOfSync, with no `syncPolicy` and no operation.
- HelmCharts refuses the prd stage, and `IaC/HelmCharts` skips both stages.
- HelmCharts' architecture build leaves KubeCoder's prd out. The next collector run is green, with
  `kubecoder-deploy` owning the ids (argocd.md's step 6).

Run D2's checks for `kubecoder-prd`: `prd` as the target revision, and `--stage prd`. Wait for
the `IaC/HelmCharts` build to finish before P5. The stops are D2's.

### P5–P6: prd's state surgery and no-destroy plan

Set `STAGE=prd` and its variables. Run [the state surgery](#the-state-surgery), then
[the no-destroy plan](#the-no-destroy-plan). prd owns no webhook, so the plan must print
`No changes. Your infrastructure matches the configuration.`

### P7–P9: replay check, pre-flight, diff review

1. [The chart replay check](#the-chart-replay-check), against P1's result. A replay lands on
   `main` and reaches `kubecoder-prd` only through a promotion. After dev has synced it, run
   `Promote-PRD` again, with `commit` empty, before the review. `prd` moves, and nothing syncs
   prd yet, because `autoSync` is off.
2. [The pre-flight](#the-pre-flight), with `<sha>` = `origin/prd`.
3. [The diff review](#the-diff-review), against prd's expected set.

### P10: prd's manual sync, by the operator alone

R9. The sync restarts every env pod in `kubecoder-prd`, this session's environment included. The
operator does the sync and its first checks alone, from a browser, not from a terminal in a prd
environment:

1. Note the resume point: **resume at P10 step 4**.
2. Sync: at `https://argocd.home`, open `kubecoder-prd`, *Sync*, then *Synchronize*.
3. Check in the UI:
   - the Application `Synced` and `Healthy`;
   - *Last Sync* `Succeeded`, with the `tf-presync-…` hook among its results;
   - the `kubecoder-controller` Deployment's pod `Running` in the resource tree;
   - `ConfigMap/kubecoder-controller-config`'s *Live Manifest* naming `prd-<n>` for `worker` and
     `vsix`.

   Then reconnect to this environment. Its return is the first env pod back.
4. The resumed session sets the shorthand and `STAGE=prd` again. It runs
   [the sync checks](#the-sync-checks), where the hook's log must show `0 added`, then
   `rm -rf $HOME/kubecoder-cutover/prd`.

If the controller does not come back, no prd environment starts either. Work from a KubeCoder
dev-stage environment (`https://kubecoder-dev.home`, served by dev's controller) or from the
operator workstation ([`operator-workstation.md`](operator-workstation.md)), and read argocd.md
[Diagnosing a failed sync](argocd.md#diagnosing-a-failed-sync). The stage is flipped, moved, and
synced in part or failed. Way back: [WB-1](#wb-1-fix-forward).

### P11: prd's `autoSync: true`

R10. Repeat D9 for prd's `release.yaml`. No prd redeploy follows now, since HelmCharts owns
neither stage. The policy must read `{"prune":true,"selfHeal":false}`.

### P12: Deploy-PRD deleted

R14 and D35. This happens only now: the promote job has retagged (P2), and prd syncs from `prd`.
The accompanying session makes the second KubeCoder edit: `Jenkinsfile.deploy-prd` is deleted.
`Build-Main`'s `Jenkinsfile` is unchanged.

Push on confirmation. Then delete the job `KubeCoder/Deploy-PRD` in
Jenkins, from the job's page with *Delete Pipeline*. The push runs Build-Main, and its build is
P13's candidate. Check it:

```sh
tags controller
curl -sg -u admin:$JENKINS_TOKEN 'https://jenkins.webathome.org/job/KubeCoder/api/json?tree=jobs[name]'
```

The first must show the new `"dev-<n>"`; the second, `Build-Main` and `Promote-PRD`
only. A red Build-Main stops the cutover; way back: [WB-1](#wb-1-fix-forward). Do not recreate
`Deploy-PRD` to recover prd: promote instead.

### P13: a promotion, a rollback, a roll-forward

R12 and D36. There are three prd syncs, in one window the operator schedules. Each restarts every
env pod in prd, this session's included, and each is handled as in P10: the operator alone, then
resume at the next step here.

Before each step, check the preconditions:

- `origin/prd` is the commit prd runs.
- No `Build-Main` build lands on `main` during the window. A new pin commit would become the
  revert's target.

1. **Promotion.** dev `Synced Healthy` at the tip of `main`, the pin commit of P12's build `<n2>`,
   is what validates it. Run `Promote-PRD` with `commit` empty: it retags `prd-<n2>`,
   fast-forwards `prd` and writes the next `release-<m>`. prd auto-syncs from the push. prd's
   controller must be at `prd-<n2>`, and the Application's `syncResult.revision` at the promoted
   SHA.
2. **Rollback.** Revert that pin commit on `main`. The session prepares the revert and pushes it
   on confirmation. `HEAD` must be the promoted pin commit before the revert.

   ```sh
   cd /work/KubeCoderDeploy && git pull --ff-only && git revert --no-edit HEAD && git show --stat HEAD
   ```

   Dev follows by auto-sync, which D36 accepts. Run `Promote-PRD`. prd goes back to the
   `prd-<n>` of the previous pin commit, retagged now if that build was never promoted. prd's
   controller must be at that `prd-<n>`: an image actually rolled back.
3. **Roll forward.** Revert the revert the same way (`git revert --no-edit HEAD`), push, then run
   `Promote-PRD`. prd is at `prd-<n2>` again, and the retag is skipped: *"exists; left as it
   is"*.

A refused promotion changes nothing, as in P2. A failed sync is handled as in P10. D36's
emergency lever, force-moving `prd` back, is not this rehearsal; it is
[WB-3](#wb-3-prds-emergency-lever).

## After both stages, unhurried

R13.

### X1: the orphaned Helm release Secrets

Argo tracks none of them. Deleting them removes Helm's record of the old release and nothing
else. Afterwards there is no `helm history` or `helm rollback` for KubeCoder, and no
[WB-2](#wb-2-hand-a-stage-back-to-jenkins). For each stage:

```sh
cexec iac kubectl $KC get secret -n kubecoder-$STAGE -l owner=helm,name=kubecoder-$STAGE
cexec iac kubectl $KC delete secret -n kubecoder-$STAGE -l owner=helm,name=kubecoder-$STAGE
cexec iac helm --kubeconfig $HOME/.kube/config-prd-write --kube-context prd list -n kubecoder-$STAGE
```

The first must list only `sh.helm.release.v1.kubecoder-<stage>.v…`, ten revisions per stage on
2026-09-22. The last must print only Helm's header row, `NAME  NAMESPACE  REVISION …`.

### X2: HelmCharts' copy and Terraform

This is one HelmCharts commit, made only after both stages' state surgery. prd's surgery and
[WB-2](#wb-2-hand-a-stage-back-to-jenkins) read `_shared/`.

```sh
cd /work/HelmCharts && git pull --ff-only && git rm -rq charts/kubecoder configs/prd/kubecoder/_shared && kc project test && git commit -m "kubecoder: KubeCoderDeploy owns the chart and its Terraform (slice 012)"
```

Push on confirmation. Nothing is stranded: `architecture.yaml` already lives in KubeCoderDeploy
(slice 014), and neither stage is HelmCharts' any more. `IaC/HelmCharts` runs and deploys nothing
for KubeCoder. The two emptied states, `helm-charts/prd/kubecoder/{dev,prd}/infra.tfstate`, stay
in TerraformState, because nothing prunes a state (D28).

HelmCharts' orphan audit is run by hand. What it lists at this step:

```sh
cd /work/HelmCharts && cexec iac poetry run audit-prd-orphans desired
```

- `kubecoder-dev` and `kubecoder-prd` are among the namespaces, and not among the Helm releases.
- **It does not list KubeCoder's storage as an orphan, before or after this commit.** It
  enumerates ZFS on `zpool2` only, and KubeCoder's datasets are on `zpool5`. The one change the
  deletion makes is that the desired `zpool2` set loses a spurious `prd`, which is the audit's
  reading of KubeCoder's conditional dataset name.
- Its `diff`, run against a `collect`, has no
  `= kubecoder-<stage>` "Owned by another reconciler" lines once X1 has deleted the Helm release
  Secrets.

### X3: the KubeCoder task, filed for after prd's cutover

D1. The operator files it in KubeCoder's own environment; it is Python with tests, plus
KubeCoderSpecs. This is its specification:

1. **Remove the controller's worker and vsix ImageVolume `"pullPolicy": "Always"` lines**, at
   `controller/src/kubecoder_controller/podcomposer.py:1718,1725` on 2026-09-21, together with
   the comments' floating-tag premise, with tests. Both stages now run from pins, so the
   stale-worker bug D145 fixed cannot come back through these images. Removing the lines before
   prd's cutover would have brought it back.
2. **Update D145** (`/work/KubeCoderSpecs/decisions.md`) from what is then live, not from what the
   chart dropped. It retires only in part: worker and vsix follow the pins, while the dev
   container, services, toolchains, samba, kaniko and localHome images stay floating and keep
   `Always`. Its checklist is stale: the third from-scratch container is
   `_image_builder_sidecar`, not `_busybox_share_init`, and `vsix` carries an ImageVolume
   `pullPolicy` too. To read the live set from an env pod:

   ```sh
   cexec iac kubectl $KC get pods -n kubecoder-prd -l homelab.local/env-id -o jsonpath='{range .items[0].spec.initContainers[*]}{.name} {.image} {.imagePullPolicy}{"\n"}{end}{range .items[0].spec.containers[*]}{.name} {.image} {.imagePullPolicy}{"\n"}{end}{range .items[0].spec.volumes[?(@.image)]}{.name} {.image.reference} {.image.pullPolicy}{"\n"}{end}'
   ```

## Exit

The exit criterion: **both stages on Argo; a full build → dev → promote → prd cycle and one
rollback done through git alone; Jenkins holds no cluster credential for KubeCoder.** These checks
prove each part:

1. **Both stages on Argo.**

   ```sh
   cexec iac kubectl $KC get applications.argoproj.io -n argocd-prd kubecoder-dev kubecoder-prd -o custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status,REV:.spec.source.targetRevision,AUTOPRUNE:.spec.syncPolicy.automated.prune
   ```

   Both must be `Synced Healthy`, at `main` and `prd` respectively, with `true`. HelmCharts' two
   `release.yaml` files must say `reconciler: argo-cd`.
2. **The cycle and the rollback, through git.** P13's `release-<m>` tags each name a commit and
   its images: `git -C /work/KubeCoderDeploy tag -n1 -l 'release-*'`. The prd Application's
   history holds those revisions:

   ```sh
   cexec iac kubectl $KC get application -n argocd-prd kubecoder-prd -o jsonpath='{range .status.history[*]}{.id} {.revision} {.deployedAt}{"\n"}{end}'
   ```

   After P10's sync by hand, it must show the promotion, the rollback and the roll-forward. Each
   is a revision `prd` fast-forwarded to, auto-synced.
3. **No Jenkins job deploys KubeCoder.** This is how *"Jenkins holds no cluster credential for
   KubeCoder"* is read. Jenkins holds no KubeCoder-specific cluster credential: `IaC/HelmCharts`
   deploys every release through the shared `iac` agent's kubeconfig. So the criterion is that
   nothing routes KubeCoder through it:

   ```sh
   git -C /work/KubeCoder pull -q --ff-only && grep -c helmDeploy /work/KubeCoder/Jenkinsfile; ls /work/KubeCoder/Jenkinsfile.deploy-prd
   curl -sg -u admin:$JENKINS_TOKEN 'https://jenkins.webathome.org/job/KubeCoder/api/json?tree=jobs[name]'
   cd /work/HelmCharts && for s in dev prd; do cexec iac poetry run deploy template prd/kubecoder --stage $s; done
   grep -nE 'credentialsId|helmDeploy|kubectl|kubeconfig' /work/KubeCoderDeploy/Jenkinsfile.promote
   ```

   These must show:

   - `0`, and no such file;
   - `Build-Main` and `Promote-PRD` only;
   - both stages refused, as `deployed by 'argo-cd', not jenkins`;
   - the promote job's one credential being the GitHub one, `5f6fbd66-…`, with no kubeconfig, no
     `helmDeploy` and no `kubectl`. It reaches the registry with `crane` over plain HTTP.

## Ways back

### WB-1: fix forward

This is the default. After a stage's registry commit, the Application is inert until its manual
sync: `autoSync` is off, and only a sync runs the hook. HelmCharts refuses the stage and nothing
else deploys it, so the stage keeps running as last deployed for as long as the fix takes.

1. Fix the cause in KubeCoderDeploy (a chart or Terraform change), get the gate green, and push.
   At prd, also promote.
2. Refresh the Application by hand if no webhook reaches it yet.
3. Resume at the step that stopped.

After the first sync the same holds. A hook failure applied nothing, and a sync-phase failure
applied part of the render (argocd.md
[Diagnosing a failed sync](argocd.md#diagnosing-a-failed-sync)). The next sync reruns the hook
against the moved state.

For a KubeCoder Jenkinsfile edit, revert its commit in `/work/KubeCoder`, lint, and push on
confirmation. Tags already pushed stay, and are harmless. A pin commit already on KubeCoderDeploy's
`main` is inert until dev's registry commit; revert it there too if it is wrong. Undoing P12 also
needs `KubeCoder/Deploy-PRD` created again by hand.

### WB-2: hand a stage back to Jenkins

This has not been exercised. It is available only while the stage's Helm release Secrets exist
(before X1) and `_shared/` does (before X2). The Application has to go without its finalizer, and
with nothing able to act on it between the steps.

1. **Stop the applicationset-controller.** While it is down, no registry push generates or changes
   any Application in the estate, so keep the window short.

   ```sh
   cexec iac kubectl $KC -n argocd-prd scale deploy/argocd-prd-applicationset-controller --replicas=0
   ```

2. **Delete the Application alone.** The namespace must still be `Active` afterwards.

   ```sh
   cexec iac kubectl $KC -n argocd-prd patch application kubecoder-$STAGE --type json -p '[{"op":"remove","path":"/metadata/finalizers"}]'
   cexec iac kubectl $KC -n argocd-prd delete application kubecoder-$STAGE
   cexec iac kubectl $KC get namespace kubecoder-$STAGE
   ```

3. **If the state surgery ran, move the storage back.** This is the surgery's recipe with the
   roles swapped. HelmCharts' key receives the addresses, so it is pushed first. The pulled
   source keeps its lineage, so the move writes to it.

   ```sh
   mkdir -p $W/src $W/dst $W/moves && printf 'terraform {\n  backend "http" {}\n}\n' | tee $W/src/backend.tf > $W/dst/backend.tf
   cd $W/src && tfinit "$SRC" && cexec iac terraform state pull > $W/moves/src.tfstate
   cd $W/dst && tfinit "$DST" && cexec iac terraform state pull > $W/moves/dst.tfstate
   cd $W/moves && cexec iac terraform state mv -state=dst.tfstate -state-out=src.tfstate homelab_zfs_dataset.env_storage module.zfs.homelab_zfs_dataset.this && cexec iac terraform state mv -state=dst.tfstate -state-out=src.tfstate kubernetes_persistent_volume_v1.env_storage module.zfs.kubernetes_persistent_volume_v1.this
   cd $W/src && cexec iac terraform state push ../moves/src.tfstate && cd $W/dst && cexec iac terraform state push ../moves/dst.tfstate && rm -rf $W/moves
   ```

   After dev's sync, KubeCoderDeploy's state also keeps `github_repository_webhook.argocd[0]`,
   and the webhook stays on GitHub. Either delete the webhook in GitHub's UI and run
   `cexec iac terraform state rm 'github_repository_webhook.argocd[0]'` in `$W/dst`, or leave
   both. Argo refreshes nothing for an app that is gone.
4. **Revert the stage's registry commits in HelmCharts.** That removes `release.yaml` and brings
   `values.yaml` back. Run `kc project test`, then push on confirmation. The push starts
   `IaC/HelmCharts`, whose infra apply for the stage fails on the existing namespace, which
   HelmCharts' state does not hold yet. That failure is expected and applies nothing else.
5. **Re-import the namespace**, now that the stage is Jenkins' again. The plan must show no
   changes.

   ```sh
   cd /work/Ansible && ( . scripts/bao-login.sh && cd /work/HelmCharts && . scripts/setup-env.sh prd && cexec iac poetry run deploy import prd/kubecoder --stage $STAGE module.namespace.kubernetes_namespace_v1.this kubecoder-$STAGE && cexec iac poetry run deploy plan prd/kubecoder --stage $STAGE )
   ```

6. **Start the applicationset-controller again.** No `kubecoder-$STAGE` Application may reappear.
   If its log shows `failed to create webhook handler`, restart it per argocd.md
   [Restarting the applicationset-controller](argocd.md#restarting-the-applicationset-controller).

   ```sh
   cexec iac kubectl $KC -n argocd-prd scale deploy/argocd-prd-applicationset-controller --replicas=1
   ```

7. **Run `IaC/HelmCharts` again.** Helm takes its fields back; Argo's tracking annotations stay
   on the objects, inert. For dev, reverting D1's edit ([WB-1](#wb-1-fix-forward)) brings back
   `Build-Main`'s deploy of dev.

### WB-3: prd's emergency lever

D36. Force-move `prd` back to the previously promoted commit. It loses nothing, since `main` is
untouched, and prd auto-syncs to it. The next promotion fast-forwards `prd` again. It is a
deliberate act outside the promote job, has not been exercised, and runs on confirmation:

```sh
cd /work/KubeCoderDeploy && git fetch -q --tags origin && git show --no-patch release-<m-1> && git push --force origin 'release-<m-1>^{commit}:refs/heads/prd'
```
