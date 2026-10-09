# SecretRotator go-live (slice 045)

This runbook brings SecretRotator up on srviac. First part: its credentials, its annotations and
its nightly job, which runs in dry run. Then a week of dry run. Then going live, one kind at a time.
[§ Wave 1](#wave-1) prepares the kinds of slice 047, [§ Wave 2](#wave-2) those of slice 049, and
[§ Wave 3](#wave-3) those of slice 052,
each before the go-live or after it.

The operator runs every step, from top to bottom. Each step gives the commands, what to hand back,
and the reading that must hold before the next step starts.

Context:

- [`secret-rotation/design.md`](../../../AnsibleSpecs/secret-rotation/design.md) and
  [`catalog.md`](../../../AnsibleSpecs/secret-rotation/catalog.md).
- [`openbao.md`](openbao.md) §5: rotation as the rotator does it, and its commands.

## Conventions

- **The operator's keystroke.** That covers every OpenBao write, every playbook run, every
  `kubectl` write, every edit on srviac, every push and every Jenkins job change. The session that
  accompanies the operator reads the full output of each step and confirms the step's reading
  before the next one starts.
- **No secret on a screen, on a command line or in a file.** A token is typed at a hidden prompt
  (`read -rs`) and piped into `bao kv put`. Every check authenticates without printing.
- **Shell.** Set this up in each bash shell that runs these steps. Re-source `bao-login.sh` once
  `bao` answers `permission denied`, which means its token has expired.

  ```sh
  cd /work/Ansible && . scripts/bao-login.sh
  bao()    { cexec iac bao "$@"; }
  k()      { cexec iac kubectl --kubeconfig "$HOME/.kube/config-prd-write" --context prd "$@" </dev/null; }
  srviac() { ssh -t ansible@srviac "sudo iac -c '$*'"; }
  ```

  `srviac` runs a command in srviac's `iac` container, from the image that carries
  `secret-rotator`, the way the VS Code tasks do. The rotator's AppRole is bound to srviac's
  address, so every `secret-rotator` command that reaches OpenBao runs there.

## Before step 1

SecretRotator's `prd` branch exists and carries slice 054, and Ansible's `main` carries slice 045,
so the `iac` image srviac pulls installs `secret-rotator` from `prd`:

```sh
git -C /work/SecretRotator ls-remote --heads origin prd
srviac 'command -v secret-rotator'
srviac 'secret-rotator stamp --help'
```

**Reading:** one `refs/heads/prd` line, then `/usr/local/bin/secret-rotator`, then the help of the
`stamp` command step 6 uses, whose options end with `--clear-expires-at`. An
`invalid choice: 'stamp'` error means the image predates slice 054: stop.

## 1 — The `rotator` AppRole and `kv/iac/rotator-approle`

The `openbao` converge declares the AppRole `rotator` and its policy, and mints the AppRole's first
secret_id. It mints for `rotator` alone. Check first:

```sh
cd /work/Ansible/ansible && cexec iac poetry run ansible-playbook playbooks/site-openbao.yml -e openbao_rotate_secret_ids=true -e '{"openbao_rotate_secret_id_roles": ["rotator"]}' --check
```

**Hand back:** the full output.

**Reading:** `failed=0`. The check reports the `rotator` policy write, the `rotator` AppRole write,
the approle mount's tuning, and `A real run would mint a fresh secret_id for rotator`. A change it
reports for any other AppRole or policy: stop.

Then the same command without `--check`, then a plain converge:

```sh
cd /work/Ansible/ansible && cexec iac poetry run ansible-playbook playbooks/site-openbao.yml -e openbao_rotate_secret_ids=true -e '{"openbao_rotate_secret_id_roles": ["rotator"]}'
cd /work/Ansible/ansible && cexec iac poetry run ansible-playbook playbooks/site-openbao.yml
```

**Hand back:** the full output of both.

**Reading.**

- The first run reads `failed=0`. Its closing message names `tmp/openbao-credentials/` with
  `rotator → kv/iac/rotator-approle (keys role_id / secret_id)`, and no other AppRole.
- The second run's recap reads `changed=0` on every host.
- Where Ansible's `main` carries slice 047, the check and both runs print `kv/rotator/oidc-auth-client
  holds no client_secret (HTTP 404)` and `OIDC provisioning skipped; the OIDC config is left
  untouched`. That is expected until W4 of [§ Wave 1](#wave-1) stores the secret. OpenBao's OIDC
  login keeps the config it has.

Capture the credentials into their leaf, then wipe the staging files:

```sh
cd /work/Ansible && bao kv put -mount=kv iac/rotator-approle role_id=@tmp/openbao-credentials/rotator-role-id secret_id=@tmp/openbao-credentials/rotator-secret-id </dev/null
shred -u tmp/openbao-credentials/*
```

**Reading:** the `kv put` answers with `version 1`.

## 2 — The ServiceAccount and `kv/iac/rotator-k8s-token`

SecretRotator's `k8s/cluster-identity.yaml` declares the `secret-rotator` ServiceAccount, its
binding to `cluster-admin`, and the Secret holding its long-lived token. Nothing reconciles it, so
it is created once, by hand. Since slice 049 the Secret has a `generateName` in place of a name,
which `apply` refuses: the `k8s-sa-token` kind replaces it yearly with one named the same way. So
it is created with `create`, and the token is read from the Secret the create names:

```sh
s=$(k create -f /work/SecretRotator/k8s/cluster-identity.yaml -o name | tee /dev/stderr | grep '^secret/')
k -n kube-system get "$s" -o jsonpath='{.data.token}' | base64 -d | wc -c
[ -n "$s" ] && k -n kube-system get "$s" -o jsonpath='{.data.token}' | base64 -d | bao kv put -mount=kv iac/rotator-k8s-token token=-
```

**Hand back:** the full output.

**Reading.**

- `serviceaccount/secret-rotator`, `clusterrolebinding.rbac.authorization.k8s.io/secret-rotator-admin`
  and `secret/secret-rotator-token-<5 characters>`. A checkout before slice 049 names the Secret
  `secret/secret-rotator-token`.
- The token's length in bytes, not `0`. `0` means the token controller has not filled the Secret
  yet: run that line again, then the `kv put`.
- The last `kv put` answers with `version 1`, one more for each `kv put` that ran before it.
- `AlreadyExists` on the ServiceAccount and the binding means step 2 ran before. A checkout
  before slice 049 reports it on the Secret too, leaves `$s` empty and writes nothing: keep the
  leaf as it is. Since slice 049 the create has still made one more token Secret, which `$s`
  names, and the `kv put` has written its token to the leaf. Delete the account's other token
  Secrets:

  ```sh
  for o in $(k -n kube-system get secret -o name | grep '^secret/secret-rotator-token' | grep -vxF "$s"); do k -n kube-system delete "$o"; done
  ```

## 3 — srviac's `secrets.yaml` entries

Steps 1 and 2 come first: a `!bao` reference to a leaf that does not exist fails every `iac`
container on srviac at start, and every `IaC/*` job with it. Add the three entries under `env:`,
as `support/iac-agent/etc/iac/secrets.example.yaml` gives them:

```sh
ssh -t ansible@srviac sudoedit /etc/iac/secrets.yaml
```

```yaml
  - name: SECRET_ROTATOR_ROLE_ID
    value: !bao kv/iac/rotator-approle#role_id
  - name: SECRET_ROTATOR_SECRET_ID
    value: !bao kv/iac/rotator-approle#secret_id
  - name: SECRET_ROTATOR_K8S_TOKEN
    value: !bao kv/iac/rotator-k8s-token#token
```

Then:

```sh
srviac 'printenv | grep -c ^SECRET_ROTATOR_'
srviac 'secret-rotator audit'
```

**Hand back:** the full output.

**Reading.**

- `3`.
- The audit runs to its end and exits 1, which is expected until step 6: no key has its entry
  yet. Each data key reads `<leaf>: rotation_<key>: missing`, the keys of the leaves of steps 1
  and 2 among them. Each old-layout key that starts with `rotation_` reads
  `<leaf>: rotation_<name>: stale: the leaf has no key '<name>'`, which blocks nothing. The
  summary line ends `0 key(s) never rotate`. Its login proves the AppRole from srviac's address,
  and its orphan check proves the ServiceAccount token.
- An error from OpenBao or from the cluster instead of findings: stop and read it.

## 4 — The Telegram bot and `rotator/telegram`

1. In Telegram, ask @BotFather (`/newbot`) for the rotator's bot. It answers with the bot's
   token.
2. Add the bot to the Homelab Alerts group.
3. Store the token:

   ```sh
   read -rs tok && printf %s "$tok" | bao kv put -mount=kv rotator/telegram token=-; unset tok
   ```

4. Read the group's chat id from the bot's updates. Being added to the group is one of them:

   ```sh
   bao kv get -mount=kv -field=token rotator/telegram </dev/null \
     | python3 -c 'import json, sys, urllib.request; t = sys.stdin.read().strip(); r = json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{t}/getUpdates"))["result"]; print(sorted({(v["chat"]["id"], v["chat"].get("title")) for u in r for v in u.values() if isinstance(v, dict) and "chat" in v}))'
   ```

5. Commit the Homelab Alerts id as `telegram_chat_id` in SecretRotator's
   `src/secret_rotator/switches.yaml`, and push it to `main`:

   ```yaml
   telegram_chat_id: <the group's id>
   ```

**Hand back:** the `kv put` answer, the chat list, and the two builds the push starts.

**Reading.**

- The `kv put` answers with `version 1`.
- The chat list holds `Homelab Alerts` with a negative id.
- `IaC/SecretRotator` passes its lint and tests, resets `prd` to the push, and starts
  `IaC/IaC Docker Image`, which is green. Until that image is built, a run has no chat id and posts
  nothing to Telegram.
- The build's last stage, `Publish dashboards`, publishes the rotator's Grafana dashboard with
  Jenkins' Grafana token, `kv/jenkins/grafana-api`. Until that token is stored, the stage fails
  and the build ends red after `prd` has moved; the go-live does not wait for it.

## 5 — The Jenkins and YouTrack tokens, and the tag

**Jenkins.** The rotator calls Jenkins as the admin account. Logged in to Jenkins as admin, add an
API token on the account's Security page, named `secret-rotator`. Store it with the account's user
id:

```sh
read -rs tok && printf %s "$tok" | bao kv put -mount=kv rotator/jenkins user=<admin-user-id> token=-; unset tok
bao kv get -mount=kv -format=json rotator/jenkins </dev/null \
  | jq -r '.data.data | "user = \"\(.user):\(.token)\""' \
  | curl -sS -K - https://jenkins.webathome.org/whoAmI/api/json | jq -r '.name, .authenticated'
```

**YouTrack.** The standing card is written as Jeeves. Make a permanent token for Jeeves, with the
YouTrack scope, on Jeeves's Account Security page. Name it `secret-rotator`, or any name none of
Jeeves's other tokens has: the `youtrack-token` kind ([§ Wave 2](#wave-2)) finds the token a leaf
holds by its name. Store it:

```sh
read -rs tok && printf %s "$tok" | bao kv put -mount=kv rotator/youtrack token=-; unset tok
```

Then share the `Rotator Standing Card` tag with Jeeves, in the tag's settings in YouTrack, so that
Jeeves sees it and can add it to an issue. The run finds the open card, and tags a new one, only
through tags its token sees. The tag marks that one card and no other: a run takes the oldest open
ANS card carrying it as the standing card and rewrites its description. Check what the token sees:

```sh
yt() { bao kv get -mount=kv -field=token rotator/youtrack </dev/null | sed 's/^/Authorization: Bearer /' | curl -sS -H @- "https://issues.webathome.org/api/$1"; }
yt 'users/me?fields=login' | jq -r .login
yt 'tags?fields=name&$top=500' | jq -r '.[].name' | grep -x 'Rotator Standing Card'
yt 'admin/projects?fields=shortName&$top=500' | jq -r '.[].shortName' | grep -x ANS
```

**Hand back:** the full output.

**Reading.**

- Each `kv put` answers with `version 1`.
- Jenkins reads the admin's user id, then `true`.
- YouTrack reads Jeeves's login, then `Rotator Standing Card`, then `ANS`. A missing tag line means
  the tag is not shared with Jeeves yet. With it missing, every run fails with
  `YouTrack shows its token no tag Rotator Standing Card`.

## 6 — The annotations, and the stamps of the new leaves

The seed is the one SecretRotator's `prd` carries, transcribed from the catalog. Its apply makes
each seed leaf's custom metadata exactly the layout of design §5: one `rotation_<key>` entry per
data key, and nothing else. It also creates the 12 marker leaves, `rotator/approle/*` and
`rotator/bootstrap/*`. Dry run first:

```sh
srviac 'secret-rotator annotate'
```

**Hand back:** the full output. It lists each leaf it would write, then under it one line per
write:

- `  create  marker leaf, data key <key>`, on a marker leaf.
- `  add     rotation_<key>=<json>`, one per data key: the key's `kind`, then its `interval`,
  `args`, `activate` and `notes` where it has them.
- `  remove  <name>=<value>`, one per other key the leaf's metadata holds.
- `  set     max_versions=20  (was <n>)`, on an automatic leaf: one with a key whose kind is
  neither `manual` nor `none`.

**Reading.**

- No line is a `change`: no entry exists before the first apply.
- The `remove` lines name only what follows. A `remove` of any other name is metadata the apply
  would delete: stop and read it.
  - The old layout: `rotation_mechanism`, `rotation_interval`, `rotation_activate`,
    `rotation_args`, `key_<key>` and `interval_<key>`.
  - The sweep's trust class: `rotation=coordinated`, `rotation=external` or
    `rotation=unrestricted`.
  - Every leaf `notes`. A note the seed keeps for a key is now in that key's entry, as
    `"notes":"…"`. The rest go: the "Transcript-migrated" provenance and the sweep's "at slice
    close" plans, those of `eso/prd/filebeat/prd/elastic-credentials` and
    `eso/prd/iot/prd/elastic-credentials` among them.
- `set     max_versions=20  (was 0)` on the automatic leaves, the 12 marker leaves among them: the
  `rotator/approle/*` markers are `approle`, the `rotator/bootstrap/*` markers `external`.
- The last line reads `would patch (dry run; --apply writes) 123 leaf(s), 12 of them new marker
  leaves; 0 unchanged, 1 absent from the store, 0 live leaf(s) not in the seed`. That is every leaf
  of the seed's 124 but the absent one, since none holds an entry yet.
- An `absent from the store` line names a seed leaf the store lacks. When it is one of the leaves
  of steps 1 to 5, finish that step first. One is expected, `jenkins/grafana-api`: Jenkins' Grafana
  token for publishing dashboards, absent until the operator creates it in Grafana and stores it,
  which the go-live does not wait for. Stored before this step, it is written with the others, and
  the last line reads `124 leaf(s)` and `0 absent from the store`. Stored after it, it is a new leaf
  ([`openbao.md`](openbao.md#a-new-leaf)).
- A `not in the seed` line names a leaf the seed does not cover yet. The apply leaves it whole, old
  keys included, and the audit reports it until the seed covers it
  ([`openbao.md`](openbao.md#a-new-leaf)).
- A `no kind in the seed, no entry: <leaf>#<key>` or `named in the seed, not held by the leaf:
  <leaf>#<key>` line names a key on which the seed and the leaf disagree: fix the seed first. The
  first is a key the apply writes no entry for, which the audit then reports as `missing`; the
  second, a seed key the leaf lacks, which gets no entry.
- No `cannot write:` line. With one, the dry run ends `nothing written` and exits 1.

**With slice 047's seed.** Where `prd` carries slice 047, the dry run also prints what wave 1
changes by design ([§ Wave 1](#wave-1)). These lines are expected, and none of them asks for a
seed fix:

- `absent from the store, skipped:` for `eso/prd/infra-statistics/prd/jenkins`,
  `rotator/keycloak-client/homelab`, `rotator/keycloak-client/homelab-dev` and
  `rotator/oidc-auth-client`, which W2 to W4 create. The four add four leaves to the seed. Each
  the store lacks adds one to the last line's `absent from the store`, and leaves its leaf count
  as it is. One the store already holds is written with the others.
- On jenkins-mcp's leaf: `named in the seed, not held by the leaf:
  eso/prd/jenkins-mcp/prd/config#token` and `…#user`, and among the leaf's writes
  `add     rotation_authorization={"kind":"random",…}`. **Do W1 before the apply**, then run the
  dry run again. The seed has no entry for the stored header `authorization`, so the apply would
  give it the leaf's kind, `random`, and `random`'s first live night would replace the header
  jenkins-mcp sends to Jenkins. After W1 the leaf reads `add     rotation_token=…` (kind
  `jenkins-token`) and `add     rotation_user={"kind":"none"}`, and these three lines are gone.

**With slice 049's seed.** Where `prd` carries slice 049, the dry run also prints `absent from the
store, skipped:` for `rotator/github` and `rotator/youtrack-token/credentials`, which
[§ Wave 2](#the-counterparts-and-the-grants) creates. Each the store lacks adds one to the last
line's `absent from the store`, and leaves its leaf count as it is. One the store already holds is
written with the others, and [wave 2's annotations](#wave-2s-annotations) stamp it. Neither asks
for a seed fix.

```sh
srviac 'secret-rotator annotate --apply'
srviac 'secret-rotator annotate' | tail -n 1
```

**Hand back:** the full output.

**Reading.**

- The apply lists the dry run's writes again, then prints `patching <n> leaf(s), 12 of them new
  marker leaves; …`, `<n>` the dry run's count. Then it prints one `patched <leaf>` or `created and
  patched <leaf>` line per leaf, and exits 0.
- The dry run after it reads `would patch (dry run; --apply writes) 0 leaf(s), 0 of them new
  marker leaves; <n> unchanged, …`.
- A `stopped at <leaf>: …` line ends the apply partway, with exit 1. The leaves before it are
  written. Run the apply again once its cause is fixed: those leaves then read unchanged.

A key without a stamp is due at once. Stamp every key of the leaves steps 1 to 5 created, each
with the date its value was written, so that none is due on the day it was made. A stamp goes into
the rotator's state leaf, `kv/rotator/state`, which the first one creates. None of the five
credentials expires, so no key takes `--expires-at`:

```sh
stamp() { local d; d=$(bao kv metadata get -mount=kv -format=json "$1" </dev/null | jq -r '.data.versions[.data.current_version | tostring].created_time[:10]')
  srviac "secret-rotator stamp $1 $2 --rotated-at $d"; }
stamp iac/rotator-approle secret_id
stamp iac/rotator-k8s-token token
stamp rotator/telegram token
stamp rotator/jenkins token
stamp rotator/youtrack token
srviac 'secret-rotator audit'
srviac 'secret-rotator plan iac/rotator-approle'
```

**Hand back:** the full output.

**Reading.**

- Five `<leaf>#<key>: rotation stamp <date>, was none` lines. Each date is the day of the step
  that wrote the leaf.
- An `error: no leaf <leaf>` or `error: no key <key> in the current version of <leaf>` line means
  that stamp wrote nothing: the leaf or key is not the one its step wrote.
- Each stamp then pushes the run state to the Pushgateway (`openbao.md` §5) and says nothing when
  the push lands. A `metrics: the state group is not pushed: <error>` line is a push that failed:
  the stamp is written all the same, and the next stamp or run pushes the state again.
- No finding line of the audit names a leaf under `rotator/` or `iac/rotator-`. The nightly run
  puts any other finding on the standing card.
- The plan of `iac/rotator-approle` reads `approle plan of secret_id · due <date>`, 14 days after
  its stamp.

## 7 — `IaC/Scheduled Secret Rotation`

Last, once steps 1 to 6 are done: the job's first build runs the rotator. The job is created
through the Jenkins API by the pipeline guide's recipe
([`new-repo.md`](../../../JenkinsPipelineUtils/docs/pages/guide/new-repo.md)). It is started on a
schedule, so its `config.xml` carries no push trigger (`<properties/>`):

```sh
cat > /tmp/config.xml <<'EOF'
<?xml version='1.1' encoding='UTF-8'?>
<flow-definition plugin="workflow-job">
  <actions/>
  <description></description>
  <keepDependencies>false</keepDependencies>
  <properties/>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition" plugin="workflow-cps">
    <scm class="hudson.plugins.git.GitSCM" plugin="git">
      <configVersion>2</configVersion>
      <userRemoteConfigs>
        <hudson.plugins.git.UserRemoteConfig>
          <url>https://github.com/pvginkel/Ansible.git</url>
          <credentialsId>5f6fbd66-b41c-405f-b107-85ba6fd97f10</credentialsId>
        </hudson.plugins.git.UserRemoteConfig>
      </userRemoteConfigs>
      <branches>
        <hudson.plugins.git.BranchSpec>
          <name>*/main</name>
        </hudson.plugins.git.BranchSpec>
      </branches>
      <doGenerateSubmoduleConfigurations>false</doGenerateSubmoduleConfigurations>
      <submoduleCfg class="empty-list"/>
      <extensions/>
    </scm>
    <scriptPath>Jenkinsfile.iac-scheduled-secret-rotation</scriptPath>
    <lightweight>true</lightweight>
  </definition>
  <triggers/>
  <disabled>false</disabled>
</flow-definition>
EOF
curl -fsS -u "$JENKINS_USER:$JENKINS_TOKEN" -X POST -H 'Content-Type: application/xml' \
    --data-binary @/tmp/config.xml "$JENKINS_URL/job/IaC/createItem?name=Scheduled%20Secret%20Rotation"
curl -fsS -u "$JENKINS_USER:$JENKINS_TOKEN" -X POST "$JENKINS_URL/job/IaC/job/Scheduled%20Secret%20Rotation/build"
```

The first build puts the file's cron on the job, and runs the rotator once, a dry run. It shares
the IaC agent's single executor with the other `IaC/*` jobs, so it may queue. Once it has finished:

```sh
job="$JENKINS_URL/job/IaC/job/Scheduled%20Secret%20Rotation"
curl -fsS -u "$JENKINS_USER:$JENKINS_TOKEN" "$job/lastBuild/api/json?tree=result" | jq -r .result
curl -fsS -u "$JENKINS_USER:$JENKINS_TOKEN" "$job/lastBuild/consoleText" | grep -m 2 'secret-rotator run, '
curl -fsS -u "$JENKINS_USER:$JENKINS_TOKEN" "$job/config.xml" | grep -A1 TimerTrigger
curl -fsS -u "$JENKINS_USER:$JENKINS_TOKEN" "$job/lastBuild/consoleText" | grep 'metrics: '
```

A few minutes later, once Prometheus has scraped the run's push, read `SecretRotatorStale` and any
silence on it:

```sh
k -n prometheus-prd exec prometheus-prd-alertmanager-0 -c alertmanager -- \
  amtool --alertmanager.url=http://localhost:9093 alert query alertname=SecretRotatorStale
k -n prometheus-prd exec prometheus-prd-alertmanager-0 -c alertmanager -- \
  amtool --alertmanager.url=http://localhost:9093 silence query alertname=SecretRotatorStale
```

**Hand back:** the full output, the build's console, the standing card and the Telegram message.

**Reading.**

- `SUCCESS`.
- `secret-rotator run, commit <sha>` names the commit `prd` held, then
  `secret-rotator run, <date> (dry run): kinds random, at most 10 rotation(s)`.
- The trigger's spec reads `30 5 * * *`.
- `metrics: pushed state, audit, nightly`. A `metrics: the <group> group is not pushed: <error>`
  line is a push that failed. It changes nothing in the run, but stop and read it: while the
  `nightly` group never lands, `SecretRotatorStale` keeps firing.
- An open ANS card tagged `Rotator Standing Card` exists, marked as a dry run, and Homelab Alerts has the
  rotator's digest, marked as a dry run.
- The alert query lists no `SecretRotatorStale`. It has fired since its rule went live, because no
  nightly run had pushed yet, and this run's push resolves it.
- The silence query lists no silence. Expire one set on the alert before the go-live, with
  `amtool silence expire <id>`: left in place, it hides the one alert that says the nightly job
  stopped.

## The dry-run week

The job runs every night at 05:30 in dry run. Each morning, read the night's console, the card and
the digest. They show the plans the run would have executed, each with its steps, and the manual
rotations that are due. A finding on the card is fixed in the seed or in the store
([`openbao.md`](openbao.md) §5). Grafana's `Secret rotation` dashboard shows the state each night
leaves, once SecretRotator's build has published it, which waits on Jenkins' Grafana token. No
alert fires in dry run but `SecretRotatorStale`. Go live after a week whose nights raised nothing
unexplained.

## Wave 1

Slice 047's kinds are `keycloak-client`, `cnpg-role`, `jenkins-token`, `jenkins-job-token` and
`grafana-admin`. They ship switched off. W1 to W5 create the leaves they need, land the pushes
held for them, and annotate. Item 5 of [§ Going live](#going-live) then enables them one at a
time.

Wave 1 starts once SecretRotator's `prd` carries slice 047, before step 6 or at any point after
it. Until W4, `srviac 'secret-rotator annotate'` then prints `absent from the store, skipped:
rotator/oidc-auth-client`. From then on, W1 comes before any `annotate --apply`, step 6's
included. W4 comes after step 1, whose check expects the converge to declare the `rotator` AppRole
and its policy, and W5 after step 6: it runs `secret-rotator` on srviac, which needs steps 1 to 3,
and it stamps with step 6's `stamp`.

Until W5's apply, the audit and the nightly card report on wave 1's keys. None of these findings
touches an enabled kind:

- `rotation_<key>: missing` on each key W1 to W4 add, and `(consumers): an orphan: …` on
  `eso/prd/infra-statistics/prd/jenkins` until W2's push has synced.
- Where step 6 ran on the seed before slice 047, also:
  - `args: realm: not homelab or homelab-dev` on each `keycloak-client` key whose entry names no
    realm;
  - `args: job: not a Jenkins job's full name` on `eso/prd/iot/prd/architecture-pipeline`;
  - `kind: unknown kind 'jenkins-admin-password': …` on `shared/jenkins/admin-password`;
  - `args: type: 'jenkins-basic-auth' is not a credential type manual documents` on jenkins-mcp's
    `rotation_authorization`, which W1 turns into `stale: the leaf has no key 'authorization'`.

### W1 — jenkins-mcp's header becomes a template

jenkins-mcp sends Jenkins the header its leaf stores as `authorization`:
`Basic <base64(user:token)>`, with the admin account's API token "Claude". JenkinsDeploy's slice
047 change has its ExternalSecret build that header from the leaf's `user` and `token`, which the
leaf does not hold yet. So the keys come first, then the push, and the stored header goes last.
The header jenkins-mcp sends stays the same throughout.

Split the stored header into the two keys, and check that they rebuild it:

```sh
bao kv get -mount=kv -format=json eso/prd/jenkins-mcp/prd/config </dev/null \
  | jq '.data.data.authorization | ltrimstr("Basic ") | @base64d | index(":") as $i | {user: .[:$i], token: .[$i+1:]}' \
  | bao kv patch -mount=kv eso/prd/jenkins-mcp/prd/config -
bao kv get -mount=kv -format=json eso/prd/jenkins-mcp/prd/config </dev/null \
  | jq -r '.data.data | .user, (.authorization == "Basic " + ("\(.user):\(.token)" | @base64))'
```

**Reading:** the patch answers with the leaf's new version, then `admin` and `true`.

Then merge JenkinsDeploy's `secret-rotator` branch, which holds the change, into `main` and push
`main`. Once Argo CD has synced it:

```sh
k -n argocd-prd get application jenkins-prd -o jsonpath='{.status.sync.revision} {.status.sync.status} {.status.health.status}{"\n"}'
k -n jenkins-prd get externalsecret jenkins-mcp-secrets -o json \
  | jq -r '(.spec.target.template.data | keys | join(" ")), (.status.conditions[] | select(.type == "Ready") | .reason)'
```

**Reading:** the pushed commit and `Synced Healthy`, then `authorization bearer-token` and
`SecretSynced`.

Last, the stored header goes. A forced sync then shows the Secret built without it:

```sh
bao kv patch -mount=kv -remove-data=authorization eso/prd/jenkins-mcp/prd/config </dev/null
bao kv get -mount=kv -format=json eso/prd/jenkins-mcp/prd/config </dev/null | jq -r '.data.data | keys | join(" ")'
k -n jenkins-prd annotate externalsecret jenkins-mcp-secrets force-sync="$(date +%s)" --overwrite
k -n jenkins-prd get externalsecret jenkins-mcp-secrets -o jsonpath='{.status.refreshTime} {.status.conditions[?(@.type=="Ready")].reason}{"\n"}'
cmp -s <(bao kv get -mount=kv -format=json eso/prd/jenkins-mcp/prd/config </dev/null | jq -j '.data.data | "Basic " + ("\(.user):\(.token)" | @base64)') \
  <(k -n jenkins-prd get secret jenkins-mcp-secrets -o jsonpath='{.data.authorization}' | base64 -d) && echo same || echo differs
```

**Reading.**

- `bearer-token token user`.
- A refresh time after the annotate, and `SecretSynced`.
- `same`: the Secret's header is the one the leaf's keys build. `differs` or another reason: stop
  and read it.

The leaf's `token` carries "Claude" over. It stays unstamped, so it falls due at once when
`jenkins-token` is enabled. That first rotation leaves "Claude" alive: you revoke it by hand
afterwards (item 8 of § Going live).

### W2 — infra-statistics' own token leaf

infra-statistics calls Jenkins with `shared/jenkins/admin-password#password`. That is no password
but the admin account's API token "OpenBao". InfraStatisticsDeploy's slice 047 change has it read
`eso/prd/infra-statistics/prd/jenkins#token` instead. The rotator creates no leaf outside
`rotator/`, so the leaf is the operator's to create, holding the same token:

```sh
bao kv get -mount=kv -field=password shared/jenkins/admin-password </dev/null | bao kv put -mount=kv eso/prd/infra-statistics/prd/jenkins token=-
bao kv get -mount=kv -format=json eso/prd/infra-statistics/prd/jenkins </dev/null \
  | jq -r '.data.data | "user = \"admin:\(.token)\""' \
  | curl -sS -K - https://jenkins.webathome.org/whoAmI/api/json | jq -r '.name, .authenticated'
```

**Reading:** `version 1`, then `admin` and `true`.

Then merge InfraStatisticsDeploy's `secret-rotator` branch, which holds the change, into `main`
and push `main`. Its sync changes the Deployment's pod template, so Argo CD restarts
infra-statistics. The new pod starts once ESO has written the token into the Secret. Once it has
synced:

```sh
k -n argocd-prd get application infra-statistics-prd -o jsonpath='{.status.sync.revision} {.status.sync.status} {.status.health.status}{"\n"}'
k -n infra-statistics-prd rollout status deployment/infra-statistics --timeout=5m
cmp -s <(bao kv get -mount=kv -field=token eso/prd/infra-statistics/prd/jenkins </dev/null) \
  <(k -n infra-statistics-prd get secret infra-statistics-secrets -o json | jq -j '.data["jenkins-token"] | @base64d') && echo same || echo differs
```

**Reading:** the pushed commit and `Synced Healthy`, then `successfully rolled out`, then `same`.

The leaf's `token` carries "OpenBao" over. It stays unstamped, so it falls due at once when
`jenkins-token` is enabled. That first rotation leaves "OpenBao" alive: you revoke it by hand
afterwards, and `shared/jenkins/admin-password` stays until then (item 8 of § Going live).

### W3 — The Keycloak counterparts

The `keycloak-client` kind logs in to each realm as a service-account client of its own, with
`manage-clients`, from the leaf `rotator/keycloak-client/<realm>`. The kind rotates
`iotsupport-admin`, so that client is not one of them. Create the client in each realm's admin
console, `homelab` on https://auth.ginbov.nl and `homelab-dev` on http://keycloak-dev.home:

1. Clients → Create client: client ID `secret-rotator`, Client authentication on, and of the
   authentication flows only Service accounts roles.
2. The client's Service accounts roles tab → Assign role → filter by clients: `realm-management`
   `manage-clients`.
3. Copy the secret from its Credentials tab, and store it:

   ```sh
   read -rs tok && printf %s "$tok" | bao kv put -mount=kv rotator/keycloak-client/homelab client_id=secret-rotator client_secret=-; unset tok
   ```

   For `homelab-dev`, the same with `rotator/keycloak-client/homelab-dev`.

Check each the way the kind uses it: a client-credentials login, then the admin API's client
lookup. `kcapi <realm> <path>` calls a realm's admin API as its counterpart:

```sh
kcapi() { local base=https://auth.ginbov.nl; [ "$1" = homelab-dev ] && base=http://keycloak-dev.home
  bao kv get -mount=kv -format=json "rotator/keycloak-client/$1" </dev/null \
    | jq -r '.data.data | "grant_type=client_credentials&client_id=\(.client_id | @uri)&client_secret=\(.client_secret | @uri)"' \
    | curl -fsS --data @- "$base/realms/$1/protocol/openid-connect/token" | jq -r .access_token \
    | sed 's/^/Authorization: Bearer /' | curl -fsS -H @- "$base/admin/realms/$1/$2"; }
kcapi homelab 'clients?clientId=openbao' | jq -r '.[].clientId'
kcapi homelab-dev 'clients?clientId=iotsupport-admin' | jq -r '.[].clientId'
```

**Reading.**

- Each `kv put` answers with `version 1`.
- `openbao`, then `iotsupport-admin`.
- `curl: (22) … 401` first means the login failed: the stored id or secret is not the client's. A
  `403` means the client lacks `manage-clients`.

### W4 — OpenBao's OIDC client secret

Since slice 047, the `openbao` role takes OpenBao's OIDC client secret from
`rotator/oidc-auth-client#client_secret`. Until that leaf holds it, `site-openbao.yml` skips OIDC
provisioning and leaves OpenBao's OIDC config as it is. Copy the secret Keycloak holds for client
`openbao` into the leaf, through W3's counterpart:

```sh
id=$(kcapi homelab 'clients?clientId=openbao' | jq -r '.[0].id')
kcapi homelab "clients/$id/client-secret" | jq -j .value | bao kv put -mount=kv rotator/oidc-auth-client client_secret=-
cmp -s <(kcapi homelab "clients/$id/client-secret" | jq -j .value) <(bao kv get -mount=kv -field=client_secret rotator/oidc-auth-client </dev/null) && echo same || echo differs
```

**Reading:** `version 1`, then `same`.

The converge then writes OpenBao's OIDC config from the leaf. It comes before `keycloak-client` is
enabled: it writes the rotator policy's `read` and `update` on `auth/oidc/config`, without which
the kind's write of the config fails after a regenerate that has no undo. Check first:

```sh
cd /work/Ansible/ansible && cexec iac poetry run ansible-playbook playbooks/site-openbao.yml --check
cd /work/Ansible/ansible && cexec iac poetry run ansible-playbook playbooks/site-openbao.yml
```

**Hand back:** the full output of both.

**Reading.**

- Neither prints `holds no client_secret`.
- `failed=0`. Where the `rotator` policy lacks the `auth/oidc/config` grant, both report a change
  for `rotator` alone: the check under `Report the policy writes a real run would perform (check
  mode)`, the run under `Write consumer policies (only when text differs)`. A change for anything
  else: stop.
- The run's `Write the OIDC config (Keycloak realm)` reads `ok`: a write that changes only the
  secret reports no change.

### W5 — Wave 1's annotations

Once W1 to W4 are done:

```sh
srviac 'secret-rotator annotate'
```

**Hand back:** the full output.

**Reading.**

- It writes no leaf but these, each one no apply has written yet:
  - `eso/prd/infra-statistics/prd/jenkins`, `rotator/keycloak-client/homelab`,
    `rotator/keycloak-client/homelab-dev` and `rotator/oidc-auth-client`. Each reads an `add` per
    key, a counterpart's `client_id` with kind `none` and its `client_secret` with
    `keycloak-client` at `365d`, and `set     max_versions=20  (was 0)`.
  - Where step 6 ran on the seed before slice 047, also the entries slice 047 changed, each a
    `change`:
    - the `realm` in the args of the `keycloak-client` keys of
      `eso/prd/{dnsmasq,electronics-inventory,fieldnotes,grafana,iot,pgadmin,zigbee2mqtt}/prd/oidc`
      and `jenkins/iotsupport-pipeline-oidc`;
    - `"job":"AaC/IoTSupport"` on `eso/prd/iot/prd/architecture-pipeline`;
    - `shared/jenkins/admin-password`'s `password`, now `manual` at interval `never`.

    jenkins-mcp's leaf then reads `add     rotation_token=…`, `add     rotation_user=…` and
    `remove  rotation_authorization=…`.
  - `jenkins/grafana-api` (slice 046), where it was stored after step 6 and not annotated since.
- The last line counts those leaves: 15 where step 6 ran on the seed before slice 047. Where it ran
  on slice 047's seed, it counts the four new leaves less those step 6 found already created: 4
  where step 6 ran before W2, none where it ran after W4. Then `0 live leaf(s) not in the seed`.
- No `absent from the store` line but `jenkins/grafana-api`'s while that leaf is not stored, and
  `jenkins/keycloak-da-admin`'s once it is deleted. No
  `no kind in the seed`, `named in the seed, not held by the leaf` or `cannot write:` line.

```sh
srviac 'secret-rotator annotate --apply'
srviac 'secret-rotator annotate' | tail -n 1
```

**Reading:** one `patched <leaf>` line per leaf of the dry run, and exit 0. The dry run after it
reads `would patch (dry run; --apply writes) 0 leaf(s), …`.

The counterparts' secrets are freshly minted: stamp them with `stamp` as step 6 defines it. The
keys that carry an existing credential over stay unstamped: `rotator/oidc-auth-client`'s, the
infra-statistics leaf's and jenkins-mcp's `token`. Each falls due at once when its kind is
enabled, and its first rotation replaces the credential.

```sh
stamp rotator/keycloak-client/homelab client_secret
stamp rotator/keycloak-client/homelab-dev client_secret
srviac 'secret-rotator audit'
```

**Hand back:** the full output.

**Reading:** two `<leaf>#client_secret: rotation stamp <date>, was none` lines, each with W3's
date. No finding line of the audit names a leaf of wave 1.

## Wave 2

Slice 049's kinds are `youtrack-token`, `github-webhook-secret`, `home-assistant-token`,
`google-sa-key`, `elastic-user`, `kubecoder-client` and `k8s-sa-token`. They ship switched off. The
sections below create the two leaves and the grants they need, annotate, read their plans, and check
from srviac what no offline run could reach. Item 11 of [§ Going live](#going-live) then enables
them one at a time.

Wave 2 starts once SecretRotator's `prd` carries slice 049, before step 6 or at any point after it.
[The counterparts and the grants](#the-counterparts-and-the-grants) need only OpenBao and three
consoles. [Wave 2's annotations](#wave-2s-annotations) come after step 6: they run `secret-rotator`
on srviac, which needs steps 1 to 3, and they stamp with step 6's `stamp`. The plans and the checks
from srviac come after them.

`kinds_enabled` gates the nightly run alone. `secret-rotator run <leaf>` runs a wave-2 plan as soon
as the image carries slice 049, whatever `switches.yaml` holds, built from the store's entries. Run
none by hand before [the checks from srviac](#the-checks-from-srviac) read as they should.

Until wave 2's apply, the audit and the nightly card report on wave 2's keys. None of these findings
touches an enabled kind:

- `rotation_token: missing` on `rotator/github` and `rotator/youtrack-token/credentials`, once they
  are stored.
- Where step 6 ran on the seed before slice 049, also these, each blocking its key's plan:
  - `args: clusters: missing; the clusters whose tokens the key holds, of dev and prd` on
    `eso/prd/kubecoder/prd/catalog`'s `rotation_kubeconfig`, `rotation_kubeconfig-dev-write` and
    `rotation_kubeconfig-prd-write`, and on `iac/rotator-k8s-token`'s `rotation_token`;
  - `args: client: missing; the KubeCoder client whose credential it is` on
    `eso/prd/fieldnotes/prd/kubecoder-controller`'s `rotation_token`.

  `eso/prd/fieldnotes/prd/github-webhook-secret` raises no finding, but until the apply its `plan`
  reads `cannot be built: … no entry the plan writes names the GitHub hook …`.

### The counterparts and the grants

The rotator creates nothing outside its own staging, so the go-live creates what wave 2 needs: two
leaves under `rotator/`, and a grant on each of two Google service accounts. Store the leaves before
[wave 2's annotations](#wave-2s-annotations). One stored after them is a new leaf
([`openbao.md`](openbao.md#a-new-leaf)): run the annotations again.

**YouTrack's Hub token.** The `youtrack-token` kind lists, mints and revokes its leaves' owners'
permanent tokens through Hub, as the operator's YouTrack admin account. Logged in to YouTrack as
that account, add a permanent token on its Account Security page with the scope YouTrack
Administration. Name it `secret-rotator-hub`, or any name none of the account's other tokens has:
the kind rotates this token too, and finds it by its name. Store it:

```sh
read -rs tok && printf %s "$tok" | bao kv put -mount=kv rotator/youtrack-token/credentials token=-; unset tok
bao kv get -mount=kv -field=token rotator/youtrack-token/credentials </dev/null | sed 's/^/Authorization: Bearer /' \
  | curl -sS -H @- 'https://issues.webathome.org/hub/api/rest/users/me?fields=login' | jq -r .login
```

**Reading:** `version 1`, then the admin account's login.

**GitHub's token.** The `github.webhook` step sets the hook's secret, pings the hook and redelivers
its deliveries with the token in `rotator/github`, which nothing else reads. Logged in to GitHub as
pvginkel, generate one under Settings → Developer settings → Personal access tokens → Fine-grained
tokens:

- named `secret-rotator`, resource owner `pvginkel`, with an expiration a year out;
- repository access only `pvginkel/Fieldnotes`;
- of the repository permissions only Webhooks, read and write. GitHub adds Metadata, read-only, to
  every token.

Note its expiration date: [wave 2's annotations](#wave-2s-annotations) stamp it. Store it, and read
the hook's config with it. `ghapi` calls GitHub's API with the token, and item 11 of
[§ Going live](#going-live) uses it again:

```sh
read -rs tok && printf %s "$tok" | bao kv put -mount=kv rotator/github token=-; unset tok
ghapi() { bao kv get -mount=kv -field=token rotator/github </dev/null | sed 's/^/Authorization: Bearer /' \
  | curl -sS -H @- -H 'Accept: application/vnd.github+json' "$@"; }
ghapi https://api.github.com/repos/pvginkel/Fieldnotes/hooks/682399688/config | jq -r .url
```

**Reading:** `version 1`, then `https://fieldnotes-hooks.webathome.org/api/webhook`. `null` means
GitHub refused the token: run the call without `| jq -r .url` and read its `message`.

**The Google grants.** Each `google-sa-key` leaf holds a key of a service account that creates its
own next key and deletes the key it replaced. Read each account and its project from its key file:

```sh
bao kv get -mount=kv -field=key_json eso/prd/calendar-support/prd/google-service-account </dev/null | jq -r '"\(.project_id) \(.client_email)"'
bao kv get -mount=kv -field=firebase-service-account.json eso/prd/media/prd/mydownloads-firebase </dev/null | jq -r '"\(.project_id) \(.client_email)"'
```

**Reading:** two lines, each a project id and an account's email. For each, in the Google Cloud
console of its project:

1. APIs & Services: enable the Identity and Access Management (IAM) API.
2. IAM & Admin → Service Accounts → the account → its Permissions tab → Grant access: the account's
   own email as principal, with the role Service Account Key Admin. That gives the account
   `iam.serviceAccountKeys.create`, `.list` and `.delete` on itself, and nothing on any other
   account.

[The checks from srviac](#the-checks-from-srviac) show both grants.

### Wave 2's annotations

Slice 049 adds two leaves to the seed and changes six entries.

```sh
srviac 'secret-rotator annotate'
```

**Hand back:** the full output.

**Reading.**

- `rotator/github` and `rotator/youtrack-token/credentials`, where no apply has written them yet,
  each read `add     rotation_token=…`: `rotator/github`'s
  `{"kind":"manual","interval":"365d","args":{"type":"github-pat"},…`, the other's
  `{"kind":"youtrack-token","interval":"365d",…`, which alone also reads `set     max_versions=20
  (was 0)`.
- Where step 6, or an apply after it, ran on a seed before slice 049, also these, each a `change`:
  - `eso/prd/fieldnotes/prd/github-webhook-secret`: `rotation_secret`, whose `activate` is now
    `eso,k8s-rollout,github-webhook:pvginkel/Fieldnotes/682399688`, was `auto`;
  - `eso/prd/fieldnotes/prd/kubecoder-controller`: `rotation_token`, now with
    `"args":{"client":"fieldnotes"}`;
  - `eso/prd/kubecoder/prd/catalog`: `rotation_kubeconfig` with `"args":{"clusters":["dev","prd"]}`,
    `rotation_kubeconfig-dev-write` with `"args":{"clusters":["dev"]}` and
    `rotation_kubeconfig-prd-write` with `"args":{"clusters":["prd"]}`;
  - `iac/rotator-k8s-token`: `rotation_token`, with `"args":{"clusters":["prd"]}`.

  Until these are applied, the four `k8s-sa-token` plans stay blocked by `args: clusters: missing`.
- Where it ran on slice 049's seed, none of the six: that apply wrote them.
- No `absent from the store` line for the two leaves above, and no `cannot write:` line.

```sh
srviac 'secret-rotator annotate --apply'
srviac 'secret-rotator annotate' | tail -n 1
```

**Reading:** one `patched <leaf>` line per leaf of the dry run, and exit 0. The dry run after it
reads `would patch (dry run; --apply writes) 0 leaf(s), …`.

Stamp the two new leaves with `stamp` as step 6 defines it, whether this apply or an earlier one
wrote their entries. A Hub token never expires. The GitHub token's expiration date is its
`expires_at`, and its key falls due 7 days before it:

```sh
stamp rotator/youtrack-token/credentials token
stamp rotator/github token
srviac 'secret-rotator stamp rotator/github token --expires-at <the token expiration date>'
srviac 'secret-rotator audit'
```

**Hand back:** the full output.

**Reading.**

- Two `<leaf>#token: rotation stamp <date>, was none` lines, each with the date its leaf was stored,
  then `rotator/github#token: expires_at <date>, was none`.
- No finding line of the audit names a leaf of wave 2.
- Wave 2's other keys have no stamp but those step 6 gave `rotator/youtrack` and
  `iac/rotator-k8s-token`. Each falls due at once when its kind is enabled.

### Wave 2's plans

Read the plans the store now builds, from srviac. Slice 049 built them only against a snapshot of
`prd`. Read each kind's again just before its commit (item 11 of [§ Going live](#going-live)):

```sh
for l in eso/prd/fieldnotes/prd/{youtrack-token,github-webhook-secret,kubecoder-controller} \
    eso/prd/youtrack/prd/{mcp,backup} jenkins/youtrack rotator/youtrack rotator/youtrack-token/credentials \
    eso/prd/homeassistant-mcp/prd/homeassistant jenkins/home-automation-fleet \
    eso/prd/calendar-support/prd/google-service-account eso/prd/media/prd/mydownloads-firebase \
    eso/prd/elasticsearch/prd/{elastic,filebeat-reader,kibana-system} eso/prd/{filebeat,iot}/prd/elastic-credentials \
    eso/prd/kubecoder/prd/catalog iac/rotator-k8s-token; do srviac "secret-rotator plan $l"; done
```

**Hand back:** the full output.

**Reading**, for each wave-2 kind's `<kind> plan of <key> · due: …`. The leaves' other plans print
too: `youtrack/prd/mcp`'s `random` plan of `bearer-token`, and those of the catalog's other keys.
Each `eso.sync` names an ExternalSecret that reads the leaf or a copy of it, and each `k8s.rollout`
a workload that consumes one. The targets below are those prd held on 2026-10-09.

- `youtrack-token`: `youtrack_token.mint` of a token named `<leaf>#<key>`, `kv.write`, the syncs and
  rollouts below, the silent `youtrack_token.prove`, then `youtrack_token.revoke` and `kv.stamp`.
  - `fieldnotes/prd/youtrack-token`: `fieldnotes-prd/fieldnotes-youtrack-token`, then
    `fieldnotes-prd/deployment/fieldnotes`.
  - `youtrack/prd/mcp`'s `youtrack-api-key`: `intercom-prd/intercom-mcp-tokens` and
    `youtrack-mcp-prd/youtrack-mcp`, then `intercom-prd/deployment/intercom` and
    `youtrack-mcp-prd/deployment/youtrack-mcp`.
  - `youtrack/prd/backup`: `youtrack-prd/youtrack-backup`, and no rollout. Its activate is `none`,
    and its sync comes before the revoke all the same.
  - The catalog's `youtrack-api-key`: `kubecoder-prd/kubecoder-secret-catalog`, then
    `kubecoder-prd/deployment/kubecoder-controller`.
  - `jenkins/youtrack`'s `admin-token`, `rotator/youtrack` and `rotator/youtrack-token/credentials`:
    neither.
- `github-webhook-secret`: `random.generate`, `kv.write`, the `eso.sync` of
  `fieldnotes-prd/fieldnotes-github-webhook-secret`, the `k8s.rollout` of
  `fieldnotes-prd/deployment/fieldnotes`, then, after that rollout, `github.webhook` on
  `pvginkel/Fieldnotes/682399688`, and `kv.stamp`.
- `kubecoder-client`: `kubecoder_client.mint` for client `fieldnotes`, `kv.write`, the `eso.sync`
  of `fieldnotes-prd/fieldnotes-kubecoder-controller`, the `k8s.rollout` of
  `fieldnotes-prd/deployment/fieldnotes`, the silent `kubecoder_client.prove`, `kv.stamp`.
- `home-assistant-token`: `home_assistant_token.mint` of a token `that expires in 90 days`,
  `kv.write`, the silent `home_assistant_token.prove`, `home_assistant_token.delete`, `kv.stamp`.
  The MCP leaf syncs `homeassistant-mcp-prd/homeassistant-mcp-token` and rolls out
  `homeassistant-mcp-prd/deployment/homeassistant-mcp` after its `kv.write`. The Jenkins leaf does
  neither: `AaC/Home Assistant Fleet` reads it when it runs.
- `google-sa-key`: `google_sa_key.mint`, `kv.write`, one `eso.sync` and one `k8s.rollout`, the
  silent `google_sa_key.prove`, `google_sa_key.delete`, `kv.stamp`. calendar-support's are
  `calendar-support-prd/calendar-support-sa-key` and `calendar-support-prd/deployment/calendar-support`,
  mydownloads' `media-prd/media-mydownloads-firebase` and `media-prd/deployment/mydownloads`.
- `elastic-user`: the silent `elastic.login`, `random.generate`, `kv.write`, the syncs below, then
  `elastic.set_password`, then the rollout below, and `kv.stamp`.
  - `elasticsearch/prd/elastic`: `elasticsearch-prd/elasticsearch-elastic`, then
    `elasticsearch-prd/deployment/elasticsearch`.
  - `elasticsearch/prd/filebeat-reader`: a `kv.copy` to the catalog's `elastic-password`,
    `elasticsearch-prd/elasticsearch-reader` and `kubecoder-prd/kubecoder-secret-catalog`, then
    `kubecoder-prd/deployment/kubecoder-controller`. Its activate is `none`.
  - `elasticsearch/prd/kibana-system`: `elasticsearch-prd/elasticsearch-kibana-system`, then
    `elasticsearch-prd/deployment/kibana`.
  - `filebeat/prd/elastic-credentials`: `elasticsearch-prd/elasticsearch-filebeat-writer` and
    `filebeat-prd/filebeat-es-credentials`, then `filebeat-prd/daemonset/filebeat`.
  - `iot/prd/elastic-credentials`: `elasticsearch-prd/elasticsearch-iotsupport` and
    `iot-prd/iot-elastic-credentials`, then `iot-prd/deployment/iotsupport`.
- `k8s-sa-token`, one plan per key:
  - `kubeconfig`: `k8s.sa_token` on dev, then on prd, `kv.write`, the `kv.copy` to
    `eso/prd/kubecoder/dev/catalog#kubeconfig`, the `eso.sync` of `kubecoder-secret-catalog` in
    `kubecoder-prd` and in `kubecoder-dev`, the `k8s.rollout` of both stages'
    `deployment/kubecoder-controller`, the silent proofs, `k8s.sa_token.delete` on dev, then on
    prd, and `kv.stamp`.
  - `kubeconfig-dev-write`: the same on dev alone, and `kubeconfig-prd-write` on prd alone.
  - `iac/rotator-k8s-token`: `k8s.sa_token` on prd, `kv.write`, the silent proof,
    `k8s.sa_token.delete` on prd, `kv.stamp`.
- A `cannot be built:` or `blocked:` line: stop and read it.

### The checks from srviac

Slice 049 tried none of the systems below from srviac. Each check makes the call that the kind's
first step makes, from srviac's `iac` container, with the rotator's own clients and the credentials
in the store, and writes nothing. A failure here would fail that kind's first plan before its
`kv.write`, with nothing changed, except GitHub's: `github-webhook-secret` reaches GitHub last,
after its `kv.write` and Fieldnotes' rollout, so a bad token there fails the plan once Fieldnotes
holds a secret the hook does not sign with, until the rollback. The check finds it before the
commit instead.

They run in one `iac` shell on srviac. In it, `check` runs a Python snippet logged in to OpenBao as
the rotator, in which `val("<leaf>#<key>")` is that key's value, never printed:

```sh
ssh -t ansible@srviac sudo iac
```

```sh
py=$(sed -n '1s/^#!//p' /usr/local/bin/secret-rotator)
check() { "$py" -c 'import os, sys
from secret_rotator.openbao import OpenBao
bao = OpenBao()
bao.login_approle(os.environ["SECRET_ROTATOR_ROLE_ID"], os.environ["SECRET_ROTATOR_SECRET_ID"])
def val(path):
    leaf, key = path.split("#")
    return bao.read(leaf).data[key]
exec(sys.argv[1])' "$1"; }
```

**Hand back:** the full output of each check below. A traceback names the call that failed and its
answer. Read it before the kind's commit.

**Hub**, before `youtrack-token`. For each leaf: whose token it holds, asked with the token itself
as the kind does, the name its value carries, and how many of the owner's tokens Hub lists by that
name. A leaf's first plan finds its token by that name, and stops before it mints unless exactly one
of the owner's tokens has it:

```sh
check 'from secret_rotator.kinds.youtrack_token import hub
admin = hub.client(val("rotator/youtrack-token/credentials#token"), None)
for p in ["eso/prd/fieldnotes/prd/youtrack-token#token", "eso/prd/youtrack/prd/mcp#youtrack-api-key",
          "eso/prd/youtrack/prd/backup#token", "jenkins/youtrack#admin-token",
          "eso/prd/kubecoder/prd/catalog#youtrack-api-key", "rotator/youtrack#token",
          "rotator/youtrack-token/credentials#token"]:
    owner, carried = hub.owner(val(p), None), hub.carried(val(p))
    if carried is None:
        print(f"{p}: {owner.login}: not of the form perm:<login>.<name>.<secret>")
        continue
    n = sum(t.name == carried[1] for t in hub.tokens(admin, owner.id))
    print(f"{p}: {owner.login}, carrying {carried[0]}, token named {carried[1]}: {n} of that name")'
```

**Reading.**

- Seven lines, `<leaf>#<key>: <owner>, carrying <owner>, token named <name>: 1 of that name`. A
  line with another count, another login carried, or `not of the form`, is a leaf whose first plan
  would stop before it mints: stop and read it.
- No two lines name the same owner and token name. Two leaves holding one token: the first one's
  plan would revoke the token the other's consumers still use. Stop.
- The last line is the counterpart's, with the admin account's login. A line with another owner is
  a leaf whose first plan proves that Hub mints a token for another user (item 11).
- A traceback from `/hub/api/rest/users/<id>/permanenttokens` means Hub refuses the counterpart:
  its scope is not YouTrack Administration. One from `/hub/api/rest/users/me` means YouTrack and
  Hub both refuse that leaf's token.

**Elasticsearch**, before `elastic-user`. Each user logs in at `http://elasticsearch.home` with its
leaf's password, as each plan's `elastic.login` does:

```sh
check 'from secret_rotator.kinds.elastic_user import BASE
from secret_rotator.kinds.elastic_user.elasticsearch import Elasticsearch
es = Elasticsearch(BASE)
for leaf, user in [("elasticsearch/prd/elastic", "elastic"), ("elasticsearch/prd/filebeat-reader", "reader"),
                   ("elasticsearch/prd/kibana-system", "kibana_system"),
                   ("filebeat/prd/elastic-credentials", "filebeat_writer"),
                   ("iot/prd/elastic-credentials", "iotsupport")]:
    print(leaf, es.authenticate(user, val(f"eso/prd/{leaf}#password"))["username"])'
```

**Reading:** five lines, each a leaf and its user: `elastic`, `reader`, `kibana_system`,
`filebeat_writer`, `iotsupport`. `HTTP 401` means Elasticsearch refuses that leaf's password: its
plan would fail at its login. A transport error means srviac does not reach `elasticsearch.home`.

**KubeCoder's controller**, before `kubecoder-client`, at `https://kubecoder.home`. The leaf's own
credential is the mint's bearer, and the mint asks the controller's client list first:

```sh
check 'from secret_rotator.kinds.kubecoder_client import BASE
from secret_rotator.kinds.kubecoder_client.kubecoder import KubeCoder
print(KubeCoder(BASE).clients(val("eso/prd/fieldnotes/prd/kubecoder-controller#token")).get("fieldnotes"))'
```

**Reading:** `minted`. `static` means the chart provisions the name, and the controller refuses to
mint it. `None` means it lists no client `fieldnotes`, and `HTTP 401` that it refuses the leaf's
credential. Each would stop the plan at its mint, before anything changes.

**Home Assistant's websocket**, before `home-assistant-token`, at
`wss://homeassistant.webathome.org/api/websocket`. Each leaf's token logs in, as its plan's mint
does, and names the token it is:

```sh
check 'from secret_rotator.kinds.home_assistant_token import homeassistant
for p in ["eso/prd/homeassistant-mcp/prd/homeassistant#token", "jenkins/home-automation-fleet#ha_token"]:
    with homeassistant.connect(val(p)) as session:
        print(p, [f"{t.name} ({t.type})" for t in session.tokens() if t.current])'
```

**Reading.**

- Two lines, each a leaf and its token, `[<name> (long_lived_access_token)]`.
- Two different names. One token for both leaves: the first one's plan would delete the token the
  other's consumer still uses. Stop.
- `login refused` names a leaf whose token Home Assistant refuses. A transport error is srviac's
  reach of the websocket, or its TLS.

**Google**, before `google-sa-key`. Each leaf's key logs in to its account at Google's token
endpoint, `https://oauth2.googleapis.com/token`, and lists the account's keys through the IAM API,
`https://iam.googleapis.com/v1`:

```sh
check 'from secret_rotator.kinds.google_sa_key import google
for p in ["eso/prd/calendar-support/prd/google-service-account#key_json",
          "eso/prd/media/prd/mydownloads-firebase#firebase-service-account.json"]:
    key = google.parse(val(p))
    ids = google.Google().login(key).key_ids()
    print(f"{p}: {key.email}, key {key.id}: {len(ids)} user-managed key(s), the leaf key listed: {key.id in ids}")'
```

**Reading.**

- Two lines, two accounts, each ending `True`.
- Each account with fewer than 10 keys: Google holds at most 10 per account, and a plan creates
  its new key before it deletes the old one.
- `HTTP 403` on the list (`GET /v1/projects/-/serviceAccounts/…/keys`) means that account lacks its
  grant, or its project the IAM API ([the grants](#the-counterparts-and-the-grants)). An error on
  `POST /token` means Google refuses the leaf's key.

**GitHub's API**, before `github-webhook-secret`, with the token of `rotator/github`. The step reads
the hook's config and its deliveries:

```sh
check 'from secret_rotator.github import GitHub
github = GitHub()
github.authenticate(val("rotator/github#token"))
print(github.hook_config("pvginkel/Fieldnotes", 682399688)["url"])
for d in github.deliveries("pvginkel/Fieldnotes", 682399688)[:5]:
    print(d["delivered_at"], d["event"], d["status_code"], "redelivery" if d["redelivery"] else "")'
```

**Reading:** `https://fieldnotes-hooks.webathome.org/api/webhook`, then up to five of the hook's
newest deliveries. `HTTP 403` or `404` means the token does not reach the hook or its deliveries:
check its repository and its Webhooks permission. The ping before its first night (item 11) proves
the write.

**The dev apiserver**, while dev is up, before the dev plans of `k8s-sa-token`. The plans reach dev
at the apiserver `kubeconfig-dev-write` names, `https://10.1.3.3:16443`, over TLS checked against
the CA it names, with its token. The check calls it the same way, then asks dev who the token is
and whether it may create, read and delete Secrets in `kube-system`:

```sh
check 'from secret_rotator.kinds.k8s_sa_token.reach import connect
from secret_rotator.kinds.k8s_sa_token.tokens import access
p = "eso/prd/kubecoder/prd/catalog#kubeconfig-dev-write"
server, ca, token = access(val(p), "dev", p)
kube = connect(token, server, ca)
print(server, kube.call("GET", "/version")[1]["gitVersion"])
review = {"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"}
print(kube.call("POST", "/apis/authentication.k8s.io/v1/selfsubjectreviews", review)[1]["status"]["userInfo"]["username"])
for verb in ("create", "get", "delete"):
    attrs = {"namespace": "kube-system", "verb": verb, "resource": "secrets"}
    review = {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview", "spec": {"resourceAttributes": attrs}}
    print(verb, "secrets in kube-system:", kube.call("POST", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", review)[1]["status"]["allowed"])'
```

**Reading.**

- `https://10.1.3.3:16443` and dev's version, then
  `system:serviceaccount:kube-system:kubecoder-rw`, then `create`, `get` and `delete`, each `True`.
- `CERTIFICATE_VERIFY_FAILED` means Python refuses the apiserver's certificate under the
  kubeconfig's CA, which must carry the IP `10.1.3.3`. The dev plans would then fail, not be
  skipped: stop.
- `False` for a verb means dev's `edit` does not grant it, and ruling D1 rests on that grant: stop.
- A transport error while dev is up means srviac does not reach `10.1.3.3:16443`.

`exit` leaves the shell.

## Wave 3

Slice 052's kinds are `pve-root-password`, `samba-user` and `step-ca-password`. They ship switched
off. The three sections below annotate their entries, make the media Samba server read
`mydownloads-user`, and put `secret-rotator-ui` on srviac. Item 11 of [§ Going live](#going-live)
then enables the kinds one at a time.

Wave 3 starts once SecretRotator's `prd` carries slice 052, before step 6 or at any point after it.
[Wave 3's annotations](#wave-3s-annotations) come after step 6: they run `secret-rotator` on
srviac, which needs steps 1 to 3.

`kinds_enabled` gates the nightly run and its manual-due lines, never the UI. `secret-rotator ui`
and `run <leaf>` build and run a wave-3 plan as soon as the image carries slice 052, whatever
`switches.yaml` holds. They build it from the store's entries, not from the seed: until
[wave 3's annotations](#wave-3s-annotations) are applied, they build a wave-3 plan from the leaf's
older entry, as that section's readings describe. Start none before then.

- `iac/proxmox#password` (`pve-root-password`), `shared/samba/users#pvginkel` (`samba-user`, the
  personal account) and `eso/prd/kubecoder/prd/step-ca-provisioner-password#password`
  (`step-ca-password`) each have an operator step. Their plans run from the UI, never at night.
- `eso/prd/media/prd/mydownloads-user#password` (`samba-user`, the app account) has none. Once
  `samba-user` is enabled, the nightly run rotates it every 14 days.

### Wave 3's annotations

Slice 052 changes three entries of the seed. `iac/proxmox` already holds `pve-root-password`, so
its entry is unchanged.

```sh
srviac 'secret-rotator annotate'
```

**Hand back:** the full output.

**Reading.**

- Where step 6, or an apply after it, ran on a seed before slice 052, the dry run writes these three
  leaves, each with one `change` line:
  - `eso/prd/kubecoder/prd/step-ca-provisioner-password`: `rotation_password`, whose `activate` is
    now `auto`, was `eso,k8s-rollout,manual:deploy StepCaDeploy and roll step-ca`. Until the apply,
    the plan ends with that stale `operator.confirm` after the controllers' restart.
  - `eso/prd/media/prd/mydownloads-user`: `rotation_password`, now with `"args":{"account":"app"}`
    and the activate `k8s-rollout:media-prd/deployment/media,media-prd/deployment/mydownloads`.
    Until the apply, the plan asks the operator to type the password, as the personal account's
    does, and restarts mydownloads alone.
  - `shared/samba/users`: `rotation_pvginkel`, whose `manual:` text gains `and restart the
    KubeCoder environments that mount them`.
- Where it ran on slice 052's seed, none of the three: that apply wrote them.
- No `cannot write:` line.

```sh
srviac 'secret-rotator annotate --apply'
srviac 'secret-rotator annotate' | tail -n 1
```

**Reading:** one `patched <leaf>` line per leaf of the dry run, and exit 0. The dry run after it
reads `would patch (dry run; --apply writes) 0 leaf(s), …`.

None of these leaves is new, so nothing is stamped. A wave-3 key without a stamp falls due at once
when its kind is enabled. Read the plans the store now builds:

```sh
for l in iac/proxmox eso/prd/media/prd/mydownloads-user shared/samba/users \
    eso/prd/kubecoder/prd/step-ca-provisioner-password; do srviac "secret-rotator plan $l"; done
```

**Hand back:** the full output.

**Reading.**

- `iac/proxmox`: `random.generate`, then `ssh.set_password` on `root@pve`, `root@pve1` and
  `root@pve2`, then `kv.write`, the `kv.copy` to the KubeCoder catalog, its `eso.sync`, the
  `k8s.rollout` of `kubecoder-prd/deployment/kubecoder-controller`, a `you` line for the
  `operator.show` that asks to store the password in Roboform, and `kv.stamp`.
- `eso/prd/media/prd/mydownloads-user`: no `you` line. `random.generate`, `kv.write`, the
  `eso.sync` of `media-prd/samba-creds`, the `k8s.rollout` of `media-prd/deployment/media`, then
  that of `media-prd/deployment/mydownloads`, and `kv.stamp`.
- `shared/samba/users`: `pvginkel`'s plan opens with a `you` line for the `operator.credential`,
  where the operator types the password, and has no `random.generate`. Then `kv.write`, six
  `eso.sync` (`kubecoder-samba-credential` in `kubecoder-dev` and `kubecoder-prd`, and the
  `<app>-passwords` of media, newsfilter, scantopdf and storage), four `k8s.rollout`
  (`media-prd/deployment/media`, `newsfilter-prd/deployment/samba`,
  `scantopdf-prd/deployment/samba` and `storage-prd/deployment/storage`), a `you` line for the
  Windows `operator.confirm`, and `kv.stamp`. `mvdbovenkamp`'s `manual` plan never falls due.
- `eso/prd/kubecoder/prd/step-ca-provisioner-password`: the silent `step_ca.read_key`, then
  `random.generate`, a `you` line for the `operator.show` that points at
  [`step-ca-bootstrap.md`](step-ca-bootstrap.md#kubecoder-jwk), then `kv.write`, the `kv.copy` to
  `eso/prd/kubecoder/dev/step-ca-provisioner-password`, two `eso.sync`, two `k8s.rollout` (the
  `kubecoder-controller` of `kubecoder-prd`, then of `kubecoder-dev`), and `kv.stamp`. No
  `operator.confirm`.
- A `cannot be built:` line: stop and read it.

### The media Samba server reads `mydownloads-user`

Before `samba-user` is enabled, the media Samba server takes mydownloads' password from OpenBao.
While it takes a chart literal, a rotation restarts mydownloads with the new password against a
server that still holds the old one, and mydownloads' share mount fails.

Slice 052's MediaDeploy change gives the media Deployment's `samba` container its
`PASSWORD_mydownloads` from the Secret `samba-creds`, which ESO builds from
`eso/prd/media/prd/mydownloads-user`. It goes live once MediaDeploy's `main` carrying it is pushed
and Argo syncs `media-prd`. The sync restarts the media pod, Plex and Samba together, once, and
leaves mydownloads running.

```sh
k -n argocd-prd get application media-prd -o jsonpath='{.status.sync.status} {.status.health.status}{"\n"}'
k -n media-prd get deploy media -o json | jq -c '.spec.template.spec.containers[] | select(.name == "samba") | .env[] | select(.name == "PASSWORD_mydownloads") | .valueFrom // "a literal"'
k -n media-prd get deploy mydownloads -o jsonpath='{.status.readyReplicas}{"\n"}'
k -n media-prd exec deploy/mydownloads -c mydownloads -- ls /mnt | head -n 3
```

**Hand back:** the full output.

**Reading.**

- `Synced Healthy`.
- `{"secretKeyRef":{"key":"password","name":"samba-creds"}}`. `"a literal"` means the change is not
  live: stop.
- `1`, then the first entries of the share mydownloads mounts at `/mnt`. An error from `ls` means
  the mount is broken: stop.

### `secret-rotator-ui` on srviac

The operator's `site.yml --limit srviac` run installs `secret-rotator-ui` (the `iac_agent` role,
through `install.sh`), and declares tmux, which it needs (the baseline role, from
`group_vars/iac_agent.yml`).
It comes before the first live `pve-root-password` or `step-ca-password` plan. Both plans restart
the prd KubeCoder controller, which restarts every prd KubeCoder environment (design R65), the one
you work from among them, and with it the SSH session the UI runs in. In the tmux session
`secret-rotator`, the UI carries on with the plan, and reattaching shows it where it is. A UI
started without it goes with the session, and the plan stops mid-step until a Resume or runs on
alone, holding the rotator's lock.

```sh
cd /work/Ansible/ansible && cexec iac poetry run ansible-playbook playbooks/site.yml --limit srviac --check
```

**Hand back:** the full output.

**Reading:** `changed` on `Sync IaCAgent checkout to the target host` (`bin/secret-rotator-ui`),
and nothing else. `Install per-host extra packages` reads `ok` while srviac's image carries tmux
already (3.4, on 2026-10-09). Read any other change before the apply. The apply is the same command
without `--check`, and its handler runs `install.sh`, which prints
`installed /usr/local/bin/secret-rotator-ui`.

```sh
ssh ansible@srviac 'command -v tmux secret-rotator-ui'
```

**Reading:** `/usr/bin/tmux`, then `/usr/local/bin/secret-rotator-ui`.

Then open the UI with `ssh -t ansible@srviac secret-rotator-ui`, or with the VS Code task
**secret-rotator ui (srviac)**, which runs it. Close that terminal without quitting the UI, then:

```sh
ssh ansible@srviac 'tmux list-sessions'
```

**Reading:** one line, for the session `secret-rotator`. Run `ssh -t ansible@srviac
secret-rotator-ui` again: it shows the same UI, on the box you left selected. Quit it with `q`, and
`tmux list-sessions` reads `no server running on …`.

**The rule.** Always start the UI with `ssh -t ansible@srviac secret-rotator-ui`, or with the VS
Code task, and after a lost session run it again to reattach. `Ctrl-b d` leaves the UI running
without you, and quitting the UI ends the session. A UI that exits with an error holds its output
until Enter.


## Going live

Each switch change is a commit to SecretRotator's `main`, in `src/secret_rotator/switches.yaml`. It
takes effect once its build (`IaC/SecretRotator`), green at its lint and tests, has reset `prd` and
`IaC/IaC Docker Image` has rebuilt the image. The next run's first line names the commit it runs.

1. **`dry_run: false`**, with `kinds_enabled: [random]` and `max_rotations_per_run: 10` as
   committed. From the next night on, the run rotates at most 10 due `random` plans a night, so the
   first pass drains over the nights after. From that night's push on, `SecretRotationFailed` and
   `SecretRotationOverdue` can fire too ([`openbao.md`](openbao.md) §5).
2. **`manual` and `external` next**, together, in the commit after `random`'s first clean night.
   They execute nothing, since a plan with an operator step never runs at night. What they turn on
   is the manual-due status, the Telegram lines and the card lines for the manual rotations and the
   `external` keys, which are all due at go-live because none has a stamp. Until then the nightly
   log only counts them among its `due key(s) of kinds not enabled`. The operator works them in
   `secret-rotator ui` ([`openbao.md`](openbao.md) §5).
3. **One kind per commit** after that, added to `kinds_enabled` once the kinds before it rotate
   cleanly.
4. **Before `approle`**, `jenkins`, `backup`, `iac-agent` and `openbao-admin` each hold at most one
   secret_id. The rotator cannot read which one their consumers hold, so its mint for a role with
   more fails before it mints:

   ```sh
   for r in jenkins backup iac-agent openbao-admin; do printf '%s ' "$r"; bao list -format=json "auth/approle/role/$r/secret-id" </dev/null | jq length; done
   ```

   **Reading:** each role reads `1`. For a role with more, `scripts/rotation/accessor_cleanup.py
   --role <role>` destroys the ones no consumer holds: dry first, then with `--apply`. The
   `approle` keys' `expires_at` start with that kind's first rotations. Until the rotator replaces
   a role's secret_id, it keeps the playbook's, which never expires.
5. **Wave 1**, once W1 to W5 of [§ Wave 1](#wave-1) are done. Its kinds go in like any other, one
   per commit, in any order, each once its own item below holds. Each item reads the plans of the
   kind's leaves against the live store, from srviac: slice 047 built them only against a
   snapshot of `prd`.

   **Reading**, for every leaf: its `<kind> plan of <key> · due: …` with the kind's steps. Each
   `eso.sync` names an ExternalSecret that reads the leaf or a copy of it, and each `k8s.rollout`
   a workload that consumes one. A `cannot be built:` line: stop and read it.
   The kind's first night reads like a night of the dry-run week: a consumer that did not come
   back is on the card.
6. **Before `keycloak-client`**, both counterparts answer `kcapi` (W3), and W4's
   `site-openbao.yml` run has read `rotator/oidc-auth-client`:

   ```sh
   for l in eso/prd/{argocd,dnsmasq,electronics-inventory,fieldnotes,grafana,iot,pgadmin,zigbee2mqtt}/prd/oidc \
       eso/prd/iot/prd/keycloak-admin eso/dev/electronics-inventory/dev/oidc \
       jenkins/{iotsupport-pipeline-oidc,keycloak-da-admin,keycloak-iotsupport-admin} \
       rotator/keycloak-client/{homelab,homelab-dev} rotator/oidc-auth-client; do srviac "secret-rotator plan $l"; done
   ```

   What its first nights do:

   - Keycloak ends a client's old secret as it makes the new one. Each consumer fails its
     Keycloak calls from the regenerate until its rollout (design §9, "Rollout windows"), and
     OpenBao's OIDC login fails until the plan's `keycloak.openbao_oidc_config`.
   - Its 14 keys at 14 days have no stamp and are all due at once, so at 10 a night the first pass
     takes two nights.
   - `jenkins/keycloak-iotsupport-admin`'s copies roll the KubeCoder controllers of `prd` and
     `dev`, which restarts every KubeCoder environment (design R65).
   - `jenkins/keycloak-da-admin` rotates only while it exists; it is to be deleted. Once it is
     gone, its `plan` reads `error: no leaf jenkins/keycloak-da-admin`.
   - `eso/dev/electronics-inventory/dev/oidc` rotates as a KV write: the dev cluster takes it when
     it next boots. Its `homelab-dev` regenerate runs all the same, since `keycloak-dev` runs on
     `prd`.
7. **Before `cnpg-role`**, read the state of the Cluster's managed roles:

   ```sh
   k -n postgres-pas-prd get cluster postgres -o json | jq '.status.managedRolesStatus | {byStatus, cannotReconcile}'
   for l in eso/{prd,dev}/postgres-pas/{pgadmin-admin,terraform-admin}; do srviac "secret-rotator plan $l"; done
   ```

   **Reading.**

   - On 2026-10-08 both `pgadmin_admin` and `terraform_admin` stood under
     `pending-reconciliation`, and `cannotReconcile` held `terraform_admin` with `could not
     perform UPDATE_MEMBERSHIPS on role terraform_admin: `. Another error under
     `cannotReconcile`: stop and read it.
   - CNPG applies a role's password only when its Cluster changes, which each `prd` plan's
     `cnpg.reconcile` makes. Per CNPG's source, the memberships failure does not hold a password
     back; no rotation has witnessed that yet. The first live rotation is the proof: its
     `cnpg.reconcile` waits within a bound, a failed wait names the role's byStatus and
     cannotReconcile, and its rollback puts the old password back.
   - The `dev` leaves rotate as KV writes: the dev cluster takes them when it next boots.
8. **Before `jenkins-token`**, W1 and W2 are done and their pushes synced. Before W1's push,
   jenkins-mcp's Secret takes its header from the stored `authorization`, not from `token`: after
   the first rotation jenkins-mcp would go on sending "Claude", and the revoke of "Claude" below
   would cut it off.

   ```sh
   for l in eso/prd/{infra-statistics/prd/jenkins,jenkins-mcp/prd/config,jenkins-telegram-bot/prd/config,kubecoder/prd/catalog,version-poller/prd/jenkins} \
       rotator/jenkins; do srviac "secret-rotator plan $l"; done
   ```

   **Reading.**

   - Each plan's `jenkins_token.revoke` reads `revoke any other token named <leaf>#<key>`, and
     names no other token.

   What its first night does:

   - The five keys without a stamp are due at once. `rotator/jenkins` falls due a year after its
     stamp of step 6.
   - The KubeCoder catalog's rotation rolls its controller, which restarts every KubeCoder
     environment (design R65), each then with the new token.
   - No rotation revokes a token the operator made by hand: each leaf's first rotation mints its
     own `<leaf>#<key>` token beside the one the leaf held before.

   After the first rotations, revoke the hand-made tokens on the admin's Security page,
   https://jenkins.webathome.org/user/admin/security/: OpenBao, Claude, JenkinsTelegramBot, wrkdev
   and VersionPoller. Revoke each once its leaf's first rotation is done and the page shows the
   token's use count no longer rising. A count that still rises means something else uses the
   token: find it before you revoke. "secret-rotator", the token of `rotator/jenkins`, goes the
   same way after that leaf's first rotation, a year after its stamp of step 6.

   Then `shared/jenkins/admin-password` goes, which nothing reads any more:

   ```sh
   k get externalsecrets.external-secrets.io -A -o json \
     | jq -r '.items[] | select(any(.spec.data[]?; .remoteRef.key == "shared/jenkins/admin-password")) | .metadata.namespace + "/" + .metadata.name'
   bao kv metadata delete -mount=kv shared/jenkins/admin-password </dev/null
   ```

   **Reading:** the query lists nothing; before W2's push it listed
   `infra-statistics-prd/infra-statistics-secrets`. The delete answers `Success! Data deleted (if
   it existed)`. From then on `annotate` reads `absent from the store, skipped:
   shared/jenkins/admin-password` until the seed drops the leaf.
9. **Before `jenkins-job-token`**, nothing is created: it calls Jenkins as `rotator/jenkins`. Its
   plan stops before any write unless `AaC/IoTSupport`'s `authToken` is the token in
   `eso/prd/iot/prd/architecture-pipeline#trigger_url`. Check that first:

   ```sh
   cmp -s <(curl -fsS -u "$JENKINS_USER:$JENKINS_TOKEN" "$JENKINS_URL/job/AaC/job/IoTSupport/config.xml" | sed -n 's:.*<authToken>\(.*\)</authToken>.*:\1:p' | tr -d '\n') \
     <(bao kv get -mount=kv -field=trigger_url eso/prd/iot/prd/architecture-pipeline </dev/null | sed -n 's/.*[?&]token=\([^&]*\).*/\1/p' | tr -d '\n') && echo same || echo differs
   srviac 'secret-rotator plan eso/prd/iot/prd/architecture-pipeline'
   ```

   **Reading.**

   - `same`. `differs`: the job takes another token than iotsupport sends, so iotsupport's
     trigger fails today. Bring the two into line before enabling the kind.
   - The plan generates, writes, syncs `iot-prd/iot-architecture-pipeline`, rolls out
     `iot-prd/deployment/iotsupport`, and then sets the job's token.

   Its rotation has a window: from iotsupport's rollout until `jenkins_job_token.set`, the job
   does not take iotsupport's new URL. A device or model change in that window triggers no
   architecture build.
10. **Before `grafana-admin`**, nothing is created: the kind logs in as the leaf's own
    `admin-user`. Its plan stops before any write unless Grafana takes the leaf's password from a
    server admin, which nothing has checked yet. The chart once generated a new password on every
    render, and Grafana keeps the one it created its database with. Check it:

    ```sh
    bao kv get -mount=kv -format=json eso/prd/grafana/prd/admin </dev/null \
      | jq -r '.data.data | "user = \"\(.["admin-user"]):\(.["admin-password"])\""' \
      | curl -sS -K - http://grafana.home/api/user | jq -r '.login, .isGrafanaAdmin'
    srviac 'secret-rotator plan eso/prd/grafana/prd/admin'
    ```

    **Reading.**

    - The admin's login, then `true`.
    - `null` twice: Grafana refused the password. Set Grafana's to the leaf's, then check again.
      The `k` helper passes no input, so the command is written out:

      ```sh
      bao kv get -mount=kv -field=admin-password eso/prd/grafana/prd/admin </dev/null \
        | cexec iac kubectl --kubeconfig "$HOME/.kube/config-prd-write" --context prd -n grafana-prd exec -i deploy/grafana -c grafana -- \
            grafana cli --homepath /usr/share/grafana --config /etc/grafana/grafana.ini admin reset-admin-password --password-from-stdin
      ```

    - The plan logs in, generates, writes, syncs `grafana-prd/grafana-admin`, and then sets the
      password in Grafana.
11. **Wave 2**, once [§ Wave 2](#wave-2)'s counterparts, grants and annotations are done. Its kinds
    go in one per commit, in any order, each once its own item below holds. Before each commit,
    read its leaves' plans from srviac again as [Wave 2's plans](#wave-2s-plans) gives them, and
    its check of [§ The checks from srviac](#the-checks-from-srviac). A wave-2 key without a stamp
    falls due at once when its kind is enabled. No kind has more than five such keys, within the
    cap of 10 a night. Each kind's first live plan is the proof of what no offline run tried. The
    next morning, the night's console shows the plan `rotated`, each of its steps with its `✓`
    line, and a consumer that did not come back is on the card.

    - **`youtrack-token`**, once the Hub check reads as it should. Its first night rotates the five
      keys without a stamp: those of `fieldnotes/prd/youtrack-token`, `youtrack/prd/mcp`,
      `youtrack/prd/backup`, `jenkins/youtrack` and the catalog's `youtrack-api-key`. The catalog's
      rotation rolls the prd KubeCoder controller, which restarts every prd KubeCoder environment
      (design R65). `rotator/youtrack` and the counterpart fall due a year after their stamps. Each
      plan revokes the token its leaf held, a hand-made one on its first rotation, and leaves a token
      named `<leaf>#<key>` on the owner's Account Security page.

      The proof (R2). Each `✓ mint a new YouTrack permanent token named <leaf>#<key> · token <id> of
      <login>, with the scope of token <id>` line is a token Hub minted and returned. One whose login
      is not the admin account's is a token minted for another user. Where the Hub check showed no
      leaf of another owner but `rotator/youtrack`, Jeeves's, run that plan by hand once after the
      first clean night, `srviac 'secret-rotator run rotator/youtrack'`. The card's client takes the
      new token at the plan's `kv.write`.

      A mint Hub refuses, or whose answer carries no token, fails the plan before its `kv.write`,
      and the run rolls it back. A mint that fails with a transport error may have left a token
      named `<leaf>#<key>` that no leaf holds. Look on the owner's Account Security page for one
      created that night, and revoke it: else a later plan of that leaf stops on two tokens of that
      name.
    - **`github-webhook-secret`**, once the GitHub check reads as it should. In the three days
      before its first night, ping the hook with `ghapi` ([§ Wave 2](#the-counterparts-and-the-grants)).
      GitHub signs the ping with the hook's current secret, and redelivers deliveries of the last
      three days:

      ```sh
      ghapi -X POST https://api.github.com/repos/pvginkel/Fieldnotes/hooks/682399688/pings
      ghapi 'https://api.github.com/repos/pvginkel/Fieldnotes/hooks/682399688/deliveries?per_page=5' \
        | jq -r '.[] | "\(.id) \(.guid) \(.delivered_at) \(.event) redelivery=\(.redelivery) \(.status_code)"'
      ```

      **Reading:** the newest line is a `ping`, `redelivery=false`, `200`: note its id. GitHub
      delivers it within seconds, so a list without it is read again. A JSON `message` from the
      ping's `POST` means the token lacks the Webhooks permission's write.

      Its first night restarts Fieldnotes, its API and its webhook relay in one pod, then sets the
      hook's secret. Its line reads `✓ set the secret of GitHub hook pvginkel/Fieldnotes/682399688
      from eso/prd/fieldnotes/prd/github-webhook-secret#secret · secret set; ping answered HTTP 200;
      no failed delivery to redeliver`. Pushes that GitHub delivered between the rollout and the
      change make it read `redelivered <n>: <guid>, …` instead.

      The proof, the morning after: redeliver the ping you sent before the rotation, which GitHub
      signed with the old secret, and read how Fieldnotes answers it:

      ```sh
      ghapi -X POST https://api.github.com/repos/pvginkel/Fieldnotes/hooks/682399688/deliveries/<the ping id>/attempts
      ghapi 'https://api.github.com/repos/pvginkel/Fieldnotes/hooks/682399688/deliveries?per_page=5' \
        | jq -r '.[] | "\(.id) \(.guid) \(.delivered_at) \(.event) redelivery=\(.redelivery) \(.status_code)"'
      ```

      **Reading:** the newest line is the redelivery, the ping's guid with `redelivery=true`.

      - `200`: GitHub signs a redelivery with the hook's current secret, so the step's redeliveries
        reach Fieldnotes.
      - `401`: GitHub re-sends the original signature. A push it delivered between Fieldnotes'
        rollout and the hook's change then stays undelivered, the gap of seconds the go-live
        accepts.

      The step does not judge its redeliveries. Each guid its line names reads the same way in the
      hook's Recent Deliveries, https://github.com/pvginkel/Fieldnotes/settings/hooks/682399688:
      its redelivered attempt answered `200` or `401`.
    - **`home-assistant-token`**, once the Home Assistant check reads as it should. Its first night
      rotates both keys: homeassistant-mcp restarts, and `AaC/Home Assistant Fleet` takes the new
      token at its next run. Each plan deletes the token its leaf held, the hand-made one on its
      first rotation. The user's long-lived tokens in Home Assistant (Profile → Security) then hold
      a `<leaf>#<key> <UTC time>` token per leaf, each expiring in 90 days, a date its key's
      `expires_at` carries. The proof: the line `✓ mint a new Home Assistant long-lived access token
      that expires in 90 days · <name>, which expires <date>` is a successor a long-lived token
      minted.
    - **`google-sa-key`**, once both grants are in place and the Google check reads as it should.
      Its first night rotates both keys: calendar-support and mydownloads restart. The proof: the
      line `✓ create a new key of the service account · key <id> of <email>` is the account keying
      itself under its grant, and the plan reaching `✓ delete the key the leaf held` is Google taking
      the new key within the silent proof's 5 minutes. A refused create fails the plan before its
      `kv.write`. The delete ends the leaf's old key alone: a key made elsewhere on the account
      stays, as the account's Keys tab in the console shows.
    - **`elastic-user`**, once the Elasticsearch check reads as it should. Its first night rotates
      the five keys. The superuser's plan restarts Elasticsearch, which its rollout gives 10
      minutes. From each `elastic.set_password` until the rollout after it, that user's consumer is
      refused (design §9): Kibana, filebeat's writes, iotsupport's, and the prd KubeCoder
      environments' reads of `reader`, whose rollout of the prd controller restarts them all (design
      R65). For the superuser it is Elasticsearch's own probes, so Elasticsearch turns unready until
      it restarts. ElasticsearchDeploy's README holds the superuser's hand procedure, in the plan's
      order.
    - **`kubecoder-client`**, once the KubeCoder check reads `minted`. Its first night rotates
      `fieldnotes/prd/kubecoder-controller`. From the mint until Fieldnotes' rollout, about a minute,
      Fieldnotes' calls to KubeCoder fail (design §9). The mint has no undo: once it ran, Abort is
      refused. A mint whose answer is lost has ended Fieldnotes' credential, and a Retry fails with
      `KubeCoder's controller refuses the credential … holds`. Then mint `fieldnotes` with another
      named client's credential, the `bot` client's of `eso/prd/kubecoder/prd/client-token-bot`,
      and write it to the leaf:

      ```sh
      bao kv get -mount=kv -field=token eso/prd/kubecoder/prd/client-token-bot </dev/null | sed 's/^/Authorization: Bearer /' \
        | curl -fsS -H @- -H 'Content-Type: application/json' --data '{"name":"fieldnotes"}' https://kubecoder.home/clients \
        | jq -j .credential | bao kv patch -mount=kv eso/prd/fieldnotes/prd/kubecoder-controller token=-
      ```

      Then Retry, in `srviac 'secret-rotator run eso/prd/fieldnotes/prd/kubecoder-controller'`.
    - **`k8s-sa-token`**, once step 2 is done and wave 2's annotations gave its four entries their
      `clusters`. Its first night rotates `kubeconfig-prd-write`. It mints a token Secret
      `kube-system/kubecoder-rw-token-<5 characters>` on prd and rolls both stages' KubeCoder
      controllers, which restarts every KubeCoder environment of both stages (design R65), each
      then with the new kubeconfig. Last it deletes the old Secret. That plan is the proof that the
      token controller fills a new Secret within the mint's minute, and that prd takes a delete with
      a uid precondition.

      `iac/rotator-k8s-token` falls due a year after its stamp of step 6. Its plan switches the
      running rotator to the new token before it deletes the old one, and the next `iac` container
      on srviac starts with the new token from the leaf. An `iac` container started before that
      rotation keeps the old token, and its cluster calls fail: start it again.

      `kubeconfig` and `kubeconfig-dev-write` need dev. While dev is off, the run skips their plans
      each night the quiet way: no rotation, no rollback, no Telegram. The card lists each, `dev
      does not answer at https://10.1.3.3:16443: GET /version: transport error: …`, which takes up
      to 30 s to say. They are due again the next night, and rotate the first night dev is up.

      Or by hand. Start dev, VM 919 on `pve` and off by default
      ([`live-infra-access.md`](../live-infra-access.md)), with `ssh root@pve qm start 919`. Once the
      dev check of [§ The checks from srviac](#the-checks-from-srviac) reads as it should, run both
      plans on srviac. The UI shows no plan without an operator step, so it cannot run them. Each
      plan restarts the KubeCoder controllers, and with them the environment you work from, so run
      them in a tmux session on srviac, which outlives yours:

      ```sh
      ssh -t ansible@srviac 'tmux new-session -s rotate "sudo iac -c \"secret-rotator run eso/prd/kubecoder/prd/catalog\"; read x"'
      ```

      Pick `kubeconfig-dev-write`'s plan and start it. Once it is done, Enter closes the session.
      Then the same for `kubeconfig`'s plan. After your environment has restarted, `ssh -t
      ansible@srviac tmux attach -t rotate` shows the run where it is. The first dev plan is the
      proof that dev's `edit` lets `kubecoder-rw` create, read and delete Secrets in `kube-system`
      (ruling D1). A refusal fails it at its mint, before its `kv.write`. Shut dev down after, with
      `ssh root@pve qm shutdown 919`.

11. **Wave 3**, once [Wave 3's annotations](#wave-3s-annotations) are applied. Its kinds go in
    one per commit, in any order, each once its own item below holds. A wave-3 plan with an
    operator step runs from the UI whether its kind is enabled or not. Enabling the kind adds its
    manual-due lines, and for `samba-user` the nightly rotation of `mydownloads-user`. Each kind's
    first live plan is the proof no offline run gives: it runs green and its consumers come back
    healthy.

    - **`pve-root-password`**. Its first live plan waits for `secret-rotator-ui` on srviac
      ([§ Wave 3](#secret-rotator-ui-on-srviac)), enabled or not, and is the proof of SSH from
      srviac's `iac` container to `pve`, `pve1` and `pve2` by their short names, as `ansible` with
      sudo, each host key checked against the homelab SSH host CA. A node it cannot reach fails its
      `ssh.set_password` before anything changes there, and Abort sets the nodes the plan changed
      back. The procedure is [`proxmox-credentials.md`](proxmox-credentials.md#rotation).
    - **`step-ca-password`**. Its first live plan waits for `secret-rotator-ui` on srviac too, and
      is the proof of the step-ca check against `ca.home`: the read of `kubecoder-jwk`'s key at
      the plan's start, then Done's check, where step-cli in the `iac` container opens the served
      `encryptedKey`. `ca.home` out of reach at the start fails the plan before it generates a
      password, and Abort then cancels it. The procedure is
      [`step-ca-bootstrap.md`](step-ca-bootstrap.md#kubecoder-jwk).
    - **`samba-user`**, only once the media Samba server reads `mydownloads-user`
      ([§ Wave 3](#the-media-samba-server-reads-mydownloads-user)): the nightly `mydownloads-user`
      plan is the one that would break the share. Its first night rotates `mydownloads-user`,
      which has no stamp. The plan restarts the media pod, so Plex and the media shares go down
      briefly, at 05:30 every 14 days, then mydownloads, which mounts the share with the new
      password. A media pod that does not come back fails the plan before mydownloads restarts,
      and the failure is on the card. The next morning, the night's console shows the plan
      `rotated`, and the checks of [§ Wave 3](#the-media-samba-server-reads-mydownloads-user) read
      as they did. `shared/samba/users#pvginkel` then has its manual-due lines and is worked in the
      UI. The operator types the new password, the plan restarts the four Samba servers, and its
      last screen asks to set the password in the Windows environments that mount the shares and
      to restart the KubeCoder environments that mount them. The rotator restarts no environment
      pod.

**The stops.** The immediate stop is disabling the job. `enable` in place of `disable` reverses it:

```sh
curl -fsS -u "$JENKINS_USER:$JENKINS_TOKEN" -X POST "$JENKINS_URL/job/IaC/job/Scheduled%20Secret%20Rotation/disable"
```

`paused: true`, committed, stops every run before it does anything, once its build has rebuilt the
image. A paused night still pushes its run health, with `secret_rotator_paused` 1, so
`SecretRotatorStale` stays quiet. A disabled job pushes nothing, and `SecretRotatorStale` fires
once its last run is 48 h old.
