# SecretRotator go-live (slice 045)

This runbook brings SecretRotator up on srviac. First part: its credentials, its annotations and
its nightly job, which runs in dry run. Then a week of dry run. Then going live, one kind at a time.
[§ Wave 1](#wave-1) prepares the kinds of slice 047, [§ Wave 2](#wave-2) those of slice 049,
[§ Wave 3](#wave-3) those of slice 052, [§ Wave 4](#wave-4) the `terraform` kind of slice 048, and
[§ The Ceph kinds](#the-ceph-kinds) those of slice 061,
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
  [ -n "$s" ] && for o in $(k -n kube-system get secret -o name | grep '^secret/secret-rotator-token' | grep -vxF "$s"); do k -n kube-system delete "$o"; done
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

**With slice 048's seed.** Where `prd` carries slice 048, the dry run also writes the ten
`rotator/terraform/*` marker leaves, each with `create  marker leaf, data key <key>`. They add ten
to the last line's leaf count and to its new marker leaves. It prints `absent from the store,
skipped: rotator/terraform/credentials` while the store lacks that leaf, which
[§ Wave 4](#the-terraform-kinds-token) creates, and which adds one to the last line's `absent from
the store`. Stored already, it is written with the others, and
[wave 4's annotations](#wave-4s-annotations) stamp it. None of these asks for a seed fix. The apply
leaves the markers unstamped: item 13 of [§ Going live](#going-live) stamps them.

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
  - `kubeconfig`: `vm.start` (`start srvk8sdev if it is off`), `k8s.sa_token` on dev, then on
    prd, `kv.write`, the `kv.copy` to `eso/prd/kubecoder/dev/catalog#kubeconfig`, the `eso.sync`
    of `kubecoder-secret-catalog` in `kubecoder-prd` and in `kubecoder-dev`, the `k8s.rollout` of
    both stages' `deployment/kubecoder-controller`, the silent proofs, `k8s.sa_token.delete` on
    dev, then on prd, and `kv.stamp`.
  - `kubeconfig-dev-write`: the same on dev alone, and `kubeconfig-prd-write` on prd alone, with
    no `vm.start`.
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
- `False` for a verb means dev's `edit` does not grant it, and design R112 rests on that grant: stop.
- A transport error while dev is up means srviac does not reach `10.1.3.3:16443`.

`exit` leaves the shell.

## Wave 3

Slice 052's kinds are `pve-root-password`, `samba-user` and `step-ca-password`. They ship switched
off. The three sections below annotate their entries, make the media Samba server read
`mydownloads-user`, and put `secret-rotator-ui` on srviac. Item 12 of [§ Going live](#going-live)
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

## Wave 4

Slice 048's kind is `terraform`, for the credentials Terraform mints in an app's Argo CD PreSync
hook. Its plan commits a new keeper to the deploy repo's `config/<stage>/rotation.tfvars` on
`main`, and waits for the sync whose hook re-mints the credential. Then it proves that the Secret
Terraform writes changed, and restarts every workload that reads that Secret. Its keys are ten
marker leaves, `rotator/terraform/<Application>/<keeper>`. It ships switched off. The sections
below create its GitHub token, annotate, read its plans and check the token from srviac. Item 13 of
[§ Going live](#going-live) then staggers the markers' first nights and enables the kind.

Wave 4 starts once SecretRotator's `prd` carries slice 048, before step 6 or at any point after it.
Slice 048 pushed the keeper files to the seven deploy repos' `main`.
[The token](#the-terraform-kinds-token) needs only OpenBao and GitHub.
[Wave 4's annotations](#wave-4s-annotations) come after step 6: they run `secret-rotator` on
srviac, which needs steps 1 to 3, and they stamp with step 6's `stamp`. The plans and the check
come after them.

`kinds_enabled` gates the nightly run alone. `secret-rotator run <leaf>` runs a `terraform` plan as
soon as the image carries slice 048, whatever `switches.yaml` holds: it commits to the deploy repo,
and the sync re-mints the credential. Run none by hand before item 13 of [§ Going live](#going-live).

Until wave 4's apply, the audit and the nightly card report `rotation_token: missing` on
`rotator/terraform/credentials` once it is stored, which touches no enabled kind. Where step 6 ran
on slice 048's seed, it created the ten markers without a stamp: the nightly log counts them among
its `due key(s) of kinds not enabled` until item 13 stamps them.

### The `terraform` kind's token

The kind's `terraform.commit_keeper` step reads and commits each keeper file through GitHub's
contents API, with the token in `rotator/terraform/credentials`, which nothing else reads. Logged in
to GitHub as pvginkel, generate one under Settings → Developer settings → Personal access tokens →
Fine-grained tokens:

- named `secret-rotator-terraform`, resource owner `pvginkel`, with an expiration a year out;
- repository access only these seven: `pvginkel/ElectronicsInventoryDeploy`, `pvginkel/IotDeploy`,
  `pvginkel/KeycloakDeploy`, `pvginkel/GuacamoleDeploy`, `pvginkel/StorageDeploy`,
  `pvginkel/YoutrackDeploy` and `pvginkel/PostgresPasDeploy`;
- of the repository permissions only Contents, read and write. GitHub adds Metadata, read-only, to
  every token.

The leaf's notes say the same, and its plan's first step, when the token is due, says it again.
Note its expiration date: [wave 4's annotations](#wave-4s-annotations) stamp it. Store it before
them. One stored after them is a new leaf ([`openbao.md`](openbao.md#a-new-leaf)): run the
annotations again.

```sh
read -rs tok && printf %s "$tok" | bao kv put -mount=kv rotator/terraform/credentials token=-; unset tok
```

**Reading:** `version 1`. [Wave 4's check](#wave-4s-check-from-srviac) reads what the token
reaches.

### Wave 4's annotations

Slice 048 adds eleven leaves to the seed: the token's and the ten markers, which the apply creates.

```sh
srviac 'secret-rotator annotate'
```

**Hand back:** the full output.

**Reading.**

- `rotator/terraform/credentials`, where no apply has written it yet, reads `add
  rotation_token={"kind":"manual","interval":"365d","args":{"type":"github-pat"},…` and no `set`
  line.
- Each marker of [wave 4's plans](#wave-4s-plans), where no apply has created it yet, reads:
  - `create  marker leaf, data key <key>`;
  - `add     rotation_<key>={"kind":"terraform","interval":"14d","args":{"repo":"pvginkel/<Repo>",
    "path":"","app":"<app>","keeper":"<keeper>","secret":"<app>/<Secret>"},"activate":"none"}`, on
    one line;
  - `set     max_versions=20  (was 0)`.
- Where step 6, or an apply after it, ran on slice 048's seed, none of these: that apply wrote
  them.
- The last line's `<n> of them new marker leaves` counts the markers no apply has created yet: 10
  where step 6 ran on a seed before slice 048.
- No `absent from the store` line for `rotator/terraform/credentials`, and no `cannot write:` line.

```sh
srviac 'secret-rotator annotate --apply'
srviac 'secret-rotator annotate' | tail -n 1
```

**Reading:** one `patched <leaf>` or `created and patched <leaf>` line per leaf of the dry run, and
exit 0. The dry run after it reads `would patch (dry run; --apply writes) 0 leaf(s), 0 of them new
marker leaves; …`.

Stamp the token with `stamp` as step 6 defines it, whether this apply or an earlier one wrote its
entry. Its expiration date is its `expires_at`, and its key falls due 7 days before it. The markers
stay unstamped until item 13 of [§ Going live](#going-live):

```sh
stamp rotator/terraform/credentials token
srviac 'secret-rotator stamp rotator/terraform/credentials token --expires-at <the token expiration date>'
srviac 'secret-rotator audit'
```

**Hand back:** the full output.

**Reading.**

- `rotator/terraform/credentials#token: rotation stamp <date>, was none`, with the date the leaf
  was stored, then `rotator/terraform/credentials#token: expires_at <date>, was none`.
- No finding line of the audit names a leaf under `rotator/terraform/`.

### Wave 4's plans

Read the plans the store now builds, from srviac. Slice 048 built the markers' plans only against a
snapshot of `prd`. `markers` lists them in the order item 13 of [§ Going live](#going-live)
staggers them, and item 13 uses it again:

```sh
markers='electronics-inventory-prd/db#password iot-prd/db#password keycloak-dev/db#password
  guacamole-prd/db#password keycloak-prd/db#password electronics-inventory-prd/s3#access_key
  iot-prd/s3#access_key storage-prd/backup_reader#access_key youtrack-prd/backups#token
  postgres-pas-prd/backups#token'
for m in credentials#token $markers; do srviac "secret-rotator plan rotator/terraform/${m%#*}"; done
```

**Hand back:** the full output.

**Reading.**

- `rotator/terraform/credentials`: `manual plan of token · due <date>`, 7 days before the token's
  expiration date. Its first step is a `you` line for the `operator.credential`.
- Each marker: `terraform plan of <key> · due: never rotated`, the plan's description, then its
  steps, none of them a `you` line, so the nightly run takes the plan:
  `terraform.marker` (silent), `terraform.commit_keeper` (`commit a new <keeper> keeper to
  pvginkel/<Repo>`), `argocd.sync` (`sync Argo CD Application <app>`), `terraform.prove_remint`
  (`prove Terraform re-minted Secret <app>/<Secret>`), `kv.write`, one `k8s.rollout` per restart
  below, and `kv.stamp` (silent). Each Secret is in the namespace named after the Application. The
  restarts are the workloads that read the Secret on 2026-10-10:

  | Leaf under `rotator/terraform/` | Key | Repo | Secret | Restarts |
  | --- | --- | --- | --- | --- |
  | `electronics-inventory-prd/db` | `password` | ElectronicsInventoryDeploy | `electronics-inventory-db` | `electronics-inventory-prd/deployment/electronics-inventory` |
  | `iot-prd/db` | `password` | IotDeploy | `iotsupport-db` | `iot-prd/deployment/iotsupport` |
  | `keycloak-dev/db` | `password` | KeycloakDeploy | `keycloak-db` | `keycloak-dev/deployment/keycloak` |
  | `guacamole-prd/db` | `password` | GuacamoleDeploy | `guacamole-db` | `guacamole-prd/deployment/guacamole` |
  | `keycloak-prd/db` | `password` | KeycloakDeploy | `keycloak-db` | `keycloak-prd/deployment/keycloak` |
  | `electronics-inventory-prd/s3` | `access_key` | ElectronicsInventoryDeploy | `s3-credentials` | `electronics-inventory-prd/deployment/electronics-inventory` |
  | `iot-prd/s3` | `access_key` | IotDeploy | `s3-credentials` | `iot-prd/deployment/iotsupport` |
  | `storage-prd/backup_reader` | `access_key` | StorageDeploy | `backup-reader-credentials` | none: the CronJob `s3-mirror` reads it |
  | `youtrack-prd/backups` | `token` | YoutrackDeploy | `youtrack-backup-upload` | none: the CronJob `youtrack-backup` reads it |
  | `postgres-pas-prd/backups` | `token` | PostgresPasDeploy | `postgres-backup-upload` | none: the CronJob `postgres-backup` reads it |

  Where nothing restarts, the `argocd.sync` itself waits for the Application to be Healthy.
- A restart the table lacks, or one it has that the plan lacks, is a workload that started or
  stopped reading the Secret since: read it.
- A `cannot be built:` line: stop and read it.

### Wave 4's check from srviac

The commit step reads two things before it commits: the Application, whose hook must apply the
marker's repo at its root from `main`, and the keeper file, with the token. Either failing fails
the plan before its commit, and the run rolls it back. The check finds it before the first night.

Each Application's source, from the workstation:

```sh
for a in electronics-inventory-prd iot-prd keycloak-dev keycloak-prd guacamole-prd storage-prd youtrack-prd postgres-pas-prd; do
  k -n argocd-prd get application "$a" -o json \
    | jq -r '[.metadata.name, .spec.source.targetRevision, (.spec.source.helm.parameters[] | select(.name | IN("hook.repo", "hook.path", "hook.stage")) | "\(.name)=\(.value)")] | join(" ")'
done
```

**Reading:** eight lines, each `<app> main hook.repo=https://github.com/pvginkel/<Repo>.git
hook.stage=<stage>`, `<Repo>` the table's, `<stage>` `dev` for `keycloak-dev` and `prd` for the
others, as on 2026-10-10. A `hook.path`, another repo or another branch than `main` fails that
marker's commit step with `Argo Application <app>'s hook applies …` or `Argo Application <app>
tracks …`: stop and read it.

Then, in the `iac` shell of [§ The checks from srviac](#the-checks-from-srviac), with its `check`,
the token reads each keeper file on `main`, as the commit step does, and parses it:

```sh
check 'from secret_rotator.github import GitHub
from secret_rotator.kinds.terraform import keepers
github = GitHub()
github.authenticate(val("rotator/terraform/credentials#token"))
for repo, stage in [("ElectronicsInventoryDeploy", "prd"), ("IotDeploy", "prd"), ("KeycloakDeploy", "dev"),
                    ("KeycloakDeploy", "prd"), ("GuacamoleDeploy", "prd"), ("StorageDeploy", "prd"),
                    ("YoutrackDeploy", "prd"), ("PostgresPasDeploy", "prd")]:
    found = github.contents(f"pvginkel/{repo}", f"config/{stage}/rotation.tfvars", "main")
    print(repo, stage, "not found" if found is None else keepers.parse(found[0])[1])'
```

**Reading.**

- Eight lines, each a repo, its stage and `{}`: the file's keeper map, empty until the repo's first
  rotation.
- `not found`: the seven repositories are private, and GitHub answers a token that lacks one as if
  the file were not there. The file is on `main` since slice 048, so the token lacks that
  repository: add it on the token's page. The plan would fail the same way, with
  `pvginkel/<Repo> has no config/<stage>/rotation.tfvars on main`.
- A traceback with `HTTP 401` means GitHub refuses the token. A `ValueError` names a keeper file
  the commit step cannot rewrite.
- The read proves no write. The first rotation's commit proves Contents' write: GitHub refuses a
  commit without it, and that plan fails before anything changed.

## The Ceph kinds

Slice 061's kinds are `rgw-admin` and `cephx`. They ship switched off. Each rotates one leaf per
cluster:

- `rgw-admin`: `shared/prd/ceph-rgw/s3` and `shared/dev/ceph-rgw/s3`, the S3 key of RGW's admin
  user `k8s`. The leaf's key adds a new key to its own user through RGW's admin API, on the storage
  backplane. The plan writes the new pair, syncs, proves the new key, and removes the old key last.
- `cephx`: `shared/prd/ceph-csi` and `shared/dev/ceph-csi`, the Ceph client that the CSI drivers
  and Argo CD's Terraform hook authenticate as. Two clients with the same caps, `client.k8s` and
  `client.k8s-b`, take turns. A rotation gives the one the leaf does not hold a new key, moves the
  leaf onto it, syncs and proves it, and ends no key. The rotator reaches Ceph over SSH as
  `ansible` to the PVE node a Ceph VM runs on, then `sudo -n qm guest exec` into the VM. Keys go in
  on standard input, never on a command line.

Neither kind restarts anything: CSI reads its Secret at each operation, and the hook at each Job
run. A dev plan starts srvk8sdev through `pve` when it is off, and shuts it down again after.

The sections below annotate, check from srviac what no offline run reached, bring dev up for its own
checks and the dev `microceph` role's converge, then switch the kinds on one at a time. They start
once SecretRotator's `prd` and Ansible's `main` carry slice 061, before step 6 or at any point
after it. [The Ceph kinds' annotations](#the-ceph-kinds-annotations) come after step 6: they run
`secret-rotator` on srviac, which needs steps 1 to 3.

`kinds_enabled` gates the nightly run alone. `secret-rotator run <leaf>` runs a Ceph plan as soon
as the image carries slice 061, whatever `switches.yaml` holds. Run none by hand before
[the Ceph checks](#the-ceph-checks-from-srviac) and [the dev cluster's](#the-dev-clusters-ceph) read
as they should.

### The Ceph kinds' annotations

Slice 061 changes one entry of each Ceph leaf: the `user_id` and `access_key_id` the seed marked
`none` now rotate with their secret, since each rotation moves the leaf to another Ceph client or
another S3 key.

```sh
srviac 'secret-rotator annotate'
```

**Hand back:** the full output.

**Reading.**

- Where step 6, or an apply after it, ran on a seed before slice 061, the dry run writes these four
  leaves, each with one `change` line:
  - `shared/prd/ceph-csi` and `shared/dev/ceph-csi`: `change
    rotation_user_id={"kind":"cephx","interval":"365d","activate":"none"}  (was {"kind":"none"})`;
  - `shared/prd/ceph-rgw/s3` and `shared/dev/ceph-rgw/s3`: `change
    rotation_access_key_id={"kind":"rgw-admin","interval":"365d","activate":"none"}  (was
    {"kind":"none"})`.

  Each leaf's other key keeps its entry, and step 6 already set its `max_versions`. Until the
  apply, each leaf's `plan` reads `cannot be built: <leaf>: a cephx plan rotates user_id and
  user_key together, not user_key`, or the `rgw-admin` equivalent for `secret_access_key`.
- Where it ran on slice 061's seed, none of the four: that apply wrote them.
- No `cannot write:` line.

```sh
srviac 'secret-rotator annotate --apply'
srviac 'secret-rotator annotate' | tail -n 1
```

**Reading:** one `patched <leaf>` line per leaf of the dry run, and exit 0. The dry run after it
reads `would patch (dry run; --apply writes) 0 leaf(s), …`.

Nothing is stamped. A Ceph key has no stamp, so it falls due at once when its kind is enabled. Read
the plans the store now builds:

```sh
for l in shared/{prd,dev}/ceph-csi shared/{prd,dev}/ceph-rgw/s3; do srviac "secret-rotator plan $l"; done
```

**Hand back:** the full output.

**Reading**, each plan `due: never rotated`:

- `shared/prd/ceph-csi`: `cephx plan of user_id, user_key`, whose text names `client.k8s and
  client.k8s-b`. Then `cephx.mint`, `kv.write`, five `eso.sync`, the silent `cephx.prove` and
  `kv.stamp`. The syncs are of `argocd-hooks/argocd-hook-credentials`,
  `ceph-csi-cephfs-prd/csi-cephfs-secret`, `ceph-csi-cephfs-prd/csi-cephfs-secret-user`,
  `ceph-csi-rbd-prd/csi-rbd-secret` and `ceph-csi-rbd-prd/csi-rbd-secret-user`. No rollout, no
  delete.
- `shared/dev/ceph-csi`: `vm.start` (`start srvk8sdev if it is off`) first, then `cephx.mint`,
  `kv.write`, four `eso.sync … on dev`, the silent `cephx.prove` and `kv.stamp`. The four syncs
  are of the same four CSI names, on the dev cluster. The plan names them itself, because no read
  of dev finds them while it is off.
- `shared/prd/ceph-rgw/s3`: `rgw-admin plan of access_key_id, secret_access_key`. Then
  `rgw_admin.mint`, `kv.write`, the `eso.sync` of `argocd-hooks/argocd-hook-credentials`, the
  silent `rgw_admin.prove`, `rgw_admin.delete` and `kv.stamp`.
- `shared/dev/ceph-rgw/s3`: `vm.start` first, then the same steps with no `eso.sync`.
- A `cannot be built:` line: stop and read it.

### The Ceph checks from srviac

Slice 061 tried none of the paths below live. Each check makes the calls that the kinds' steps
make, from srviac's `iac` container, with the rotator's own clients and the credentials in the
store. No check prints a key. Two of them write, each to a key no one holds:

- the import gives `client.k8s-b` a key that exists only in the check's process. That is what the
  first rotation's mint does to it before it writes the leaf, and that rotation re-keys it again.
- the self-removal adds an S3 key to `k8s` and removes it again.

They run in one `iac` shell on srviac, with `py` and `check` as
[§ The checks from srviac](#the-checks-from-srviac) defines them. `C` names the cluster: first
`prd`, then `dev` in [the dev cluster's checks](#the-dev-clusters-ceph):

```sh
export C=prd
```

**Hand back:** the full output of each check. A traceback names the call that failed and its answer.

**The PVE nodes.** Each kind asks the first PVE node that answers which node each VM is on, by
`sudo -n pvesh` as `ansible`:

```sh
check 'from secret_rotator.vmsteps import Pve
pve = Pve()
for name in ("srvceph1", "srvceph2", "srvceph3", "srvk8sdev"):
    print(pve.find(name))'
```

**Reading.**

- Four `Guest(name=…, node=…, vmid=…, status=…)` lines: `srvceph1`, `srvceph2` and `srvceph3`,
  VMs 113, 114 and 115, each `running`; `srvk8sdev`, VM 919 on `pve`, `stopped` while dev is off.
  On 2026-10-10 the three Ceph VMs ran on `pve1`, `pve2` and `pve`. Wherever they run now, the
  kinds look the node up at each use.
- `no PVE node lists the cluster's VMs: on pve it exited 255: …`: the iac container's SSH to the
  PVE nodes fails at its key, the host CA or the login. `exited 1: sudo: a password is required`:
  sudo. Stop.

**The guest agent.** For each Ceph VM in turn, the CLI runs through `sudo -n qm guest exec`, once
without standard input. Then it runs as `cephx.prove` runs it: as the leaf's client, with the
leaf's key passed on standard input (`--pass-stdin`). Last, the check compares Ceph's key for that
client with the leaf's, as the mint does:

```sh
check 'import dataclasses
from secret_rotator.vmsteps import Pve
from secret_rotator.kinds.cephx import SITES
from secret_rotator.kinds.cephx.ceph import Ceph
c = os.environ["C"]
leaf, pve = f"shared/{c}/ceph-csi", Pve()
site = SITES[leaf]
user, key = val(f"{leaf}#user_id"), val(f"{leaf}#user_key")
for name in site.guests:
    ceph = Ceph(dataclasses.replace(site, guests=(name,)), pve)
    fsid, proof = ceph.run(["fsid"]).strip(), ceph.authenticates(f"client.{user}", key)
    print(name, fsid, proof, f"client.{user}", ceph.entity(f"client.{user}").key == key)'
```

**Reading.**

- One line per Ceph VM, three on prd: `srvceph<n> <fsid> <fsid> client.k8s True`, the cluster's
  fsid twice. The second fsid is the monitors taking the leaf's key from standard input.
- `no Ceph VM of prd answers: qm guest exec <vmid> on <node> exited …`: the hop failed. Its last
  line names what refused: ssh, sudo (`a password is required`), or qm (an unknown option, no guest
  agent running). Stop.
- `ceph --name client.k8s fsid in srvceph<n> exited 1` with nothing after it: the `read` got no
  standard input, so `--pass-stdin` does not pass it through. With a message after it: the monitors
  refuse the leaf's key. Stop.
- `False`: Ceph's `client.k8s` does not hold the leaf's key, and the plan's mint would stop before
  any change. Stop, and find which of the two is current.

**The monitors' sessions.** The mint's check: every monitor's client sessions, by entity, each
named by the host whose address it comes from, as a blocked rotation names them:

```sh
check 'from secret_rotator.vmsteps import Pve
from secret_rotator.kinds.cephx import SITES
from secret_rotator.kinds.cephx.ceph import HOST_VARS, Ceph, where
c = os.environ["C"]
site = SITES[f"shared/{c}/ceph-csi"]
ceph = Ceph(site, Pve())
for entity in site.pair:
    found = ceph.clients(f"client.{entity}")
    print(f"client.{entity}:", found, where(found, HOST_VARS))'
```

**Reading.**

- `client.k8s:` the backplane addresses of the k8s nodes whose pods mount Ceph volumes
  (`192.168.188.27` to `.30` on prd), then the same nodes by name, `srvk8s1` to `srvk8s4`.
- `client.k8s-b: [] []`: no client uses it.
- An address that stays an address in the names: a client from a host no host_vars name. A blocked
  rotation would name it the same way: find what it is.
- `client.k8s: [] []` while prd's CSI volumes are mounted: the sessions do not carry the entity the
  check reads, so the check would never block. Stop.
- `mon.<name> printed no list of sessions`, or a failing `tell`: every plan would fail at its mint,
  before any change. Stop.

**The import.** The mint's import and the proof's authentication, with a new key. Run it only while
the sessions check lists no client on `client.k8s-b`, before the kind's first rotation on that
cluster. It refuses to run otherwise:

```sh
check 'import datetime
from secret_rotator.vmsteps import Pve
from secret_rotator.kinds.cephx import SITES
from secret_rotator.kinds.cephx.ceph import Ceph, keyring, new_key
c = os.environ["C"]
leaf = f"shared/{c}/ceph-csi"
site, user = SITES[leaf], val(f"{leaf}#user_id")
ceph, idle = Ceph(site, Pve()), site.other(user, leaf)
if found := ceph.clients(f"client.{idle}"):
    raise SystemExit(f"client.{idle} has clients at {found}: nothing imported")
active = ceph.entity(f"client.{user}")
key = new_key(datetime.datetime.now(datetime.UTC))
ceph.run(["auth", "import", "-i", "-"], keyring(f"client.{idle}", key, active.caps))
imported = ceph.entity(f"client.{idle}")
print(f"client.{idle}", imported.key == key, imported.caps == active.caps, ceph.authenticates(f"client.{idle}", key))'
```

**Reading.**

- `client.k8s-b True True <fsid>`. Ceph took the keyring from standard input, with the AES key the
  rotator makes and the active client's caps, and reads them back as the mint verifies them. Then
  the monitors take the new key as the proof sends it. On prd, the import created `client.k8s-b`.
  On dev it replaced the key that the converge gave it.
- `ceph auth import -i - in <VM> exited …`: Ceph refuses the keyring, and each plan would fail at
  its mint, its leaf unchanged. On dev this is also squid refusing an AES key. Stop.
- `False` first: Ceph holds another key than the one imported. `False` second: other caps than the
  active client's. Each plan would fail at its mint after the import, its leaf unchanged. Stop.

**RGW's admin API.** Each RGW instance on the backplane lists the admin user's keys, signed with
the leaf's key by the rotator's own client (`kinds/rgw_admin/rgw.py`). That is the call each step
of the plan makes first:

```sh
check 'from secret_rotator.kinds.rgw_admin import SITES
from secret_rotator.kinds.rgw_admin.rgw import Gateway, Key, Unanswered
c = os.environ["C"]
leaf = f"shared/{c}/ceph-rgw/s3"
site = SITES[leaf]
key = Key(val(f"{leaf}#access_key_id"), val(f"{leaf}#secret_access_key"))
gateway = Gateway(site)
for endpoint in site.endpoints:
    try:
        keys = gateway.admin(endpoint, key).keys(site.uid)
    except Unanswered as e:
        print(endpoint, e.error)
        continue
    print(endpoint, f"{site.uid}: {len(keys)} S3 key(s), the leaf key among them: {key.access in keys}")'
```

**Reading.**

- On prd, three lines, `http://192.168.188.24:7480`, `.25` and `.26`, each `k8s: <n> S3 key(s), the
  leaf key among them: True`. A plan removes the leaf's old key alone, so any other key of `k8s`
  stays.
- `HTTP 403: SignatureDoesNotMatch`: RGW computes another signature than the rotator's client.
  Every plan would fail at its mint, before any change. Stop.
- `HTTP 403: InvalidAccessKeyId`, or `False`: RGW does not hold the leaf's key as `k8s`'s. Stop.
- `<endpoint> GET …: transport error: …` as an instance's line: srviac does not reach that instance
  on the backplane, and the check goes on to the next. A plan passes over an instance that takes no
  connection, so one down is no stop. All three down is: read why.

**The self-removal.** The mint's add, then its undo's removal, each signed with the leaf's key: the
admin user removing a key of its own. The rotator has not seen RGW allow that:

```sh
check 'from secret_rotator.kinds.rgw_admin import SITES
from secret_rotator.kinds.rgw_admin.rgw import Gateway, Key
c = os.environ["C"]
leaf = f"shared/{c}/ceph-rgw/s3"
site = SITES[leaf]
key = Key(val(f"{leaf}#access_key_id"), val(f"{leaf}#secret_access_key"))
admin, before = Gateway(site).user(key)
(added,) = [k.access for k in admin.add_key(site.uid) if k.access not in before]
print("added", added, "at", admin.endpoint, flush=True)
admin.remove_key(site.uid, added)
after = admin.keys(site.uid)
print(f"removed: {added not in after}; the leaf key kept: {key.access in after}")'
```

**Reading.**

- `added <access key id> at http://192.168.188.24:7480`, then `removed: True; the leaf key kept:
  True`. The added key's secret was never printed or stored.
- A traceback from `PUT …/admin/user?key`: RGW refuses the add. Each plan would fail at its mint,
  before any change. Stop.
- A traceback from `DELETE …/admin/user?key` after the `added` line: RGW refuses the admin user
  removing a key of its own. A plan would then fail at `rgw_admin.delete`. Its rollback would fail
  at the mint's undo in the same way, the leaf back on the old key and both keys valid. Do not
  enable `rgw-admin`: stop. Remove the added key by hand through the Ceph VM's guest agent, the
  node and VM id from [the PVE nodes](#the-ceph-checks-from-srviac). Run it in the pod, in a shell
  set up as [§ Conventions](#conventions) gives, not in the `iac` shell on srviac:
  `~/.ssh/id_ed25519_pve` is the pod's root key to the PVE nodes, which srviac's `iac` container
  does not hold. The command takes the access key id, which is not a secret. `radosgw-admin`
  prints the user's keys, secrets included, so `jq` shows its exit code and its errors alone:

  ```sh
  ssh -i ~/.ssh/id_ed25519_pve root@<node> "qm guest exec <vmid> --timeout 60 -- microceph.radosgw-admin key rm --uid=k8s --key-type=s3 --access-key=<access key id>" \
    | jq '{exitcode: .exitcode, err: .["err-data"]}'
  ```

### The dev cluster's Ceph

Dev is off by default. The dev plans start srvk8sdev through `pve` and shut it down again. Here the
check does the same through the rotator's own `Pve`, to try that path, and dev's checks run while
it is up. Keep the `iac` shell of [the Ceph checks](#the-ceph-checks-from-srviac) open beside a
shell set up as [§ Conventions](#conventions) gives: the converge and the reads of the dev cluster
run there.

**The start**, as `vm.start` sends it, `sudo -n qm start 919` on the VM's node:

```sh
check 'from secret_rotator.vmsteps import Pve
pve = Pve()
guest = pve.find("srvk8sdev")
if guest.status != "running":
    pve.start(guest)
print(guest, pve.find("srvk8sdev").status)'
```

**Reading:** `Guest(name='srvk8sdev', node='pve', vmid=919, status='stopped') running`. A
traceback from `qm start 919 on pve`: PVE refuses the start, and every dev plan would fail at its
`vm.start`. Stop.

**What `vm.start` waits for**: dev's apiserver, dev's Ceph through the guest agent, and dev's RGW:

```sh
check 'from secret_rotator.vmsteps import Pve
from secret_rotator.kinds.cephx import SITES as CEPHX
from secret_rotator.kinds.cephx.ceph import Ceph
from secret_rotator.kinds.rgw_admin import SITES as RGW
from secret_rotator.kinds.rgw_admin.rgw import Gateway
from secret_rotator.kinds.k8s_sa_token.reach import Dev, connect
print("apiserver:", Dev(connect).unanswered(bao))
print("ceph:", Ceph(CEPHX["shared/dev/ceph-csi"], Pve()).unanswered())
print("rgw:", Gateway(RGW["shared/dev/ceph-rgw/s3"]).unanswered())'
```

**Reading:** `None` three times. While dev boots, a line says why that part does not answer: run
the check again a minute later. A dev plan's `vm.start` waits up to 15 min for all three. If one
still does not answer after that, the nightly run would skip the dev plans: read it before going on.

**The converge** of the dev `microceph` role, from `ansible/` in the pod. It declares
`client.k8s-b` with `client.k8s`'s caps. It needs SSH to srvk8sdev, whose host certificate lapses
while it is off:

```sh
cd /work/Ansible/ansible && cexec iac poetry run ansible-playbook playbooks/site-ceph.yml --limit srvk8sdev
```

**Reading.**

- `changed` on `Create missing cephx users`, for `client.k8s-b` alone, where no converge, import or
  rotation has created it yet. That gives it a key Ceph makes, which no one holds, and the import
  below replaces it. Read any other change.
- Host key verification failing: srvk8sdev's host certificate lapsed while it was off. Re-issue it
  with `-e reissue_target=srvk8sdev` ([`ssh-host-cert-expiry.md`](ssh-host-cert-expiry.md)), then
  converge again.

**Dev's readers**, from the pod, with the base kubeconfig's read-only `dev` context:

```sh
kd() { cexec iac kubectl --context dev "$@" </dev/null; }
kd get externalsecrets.external-secrets.io -A -o json | jq -r '.items[] | . as $es
  | [.spec.data[]?.remoteRef.key, (.spec.dataFrom[]?.extract.key // empty)]
  | map(select(startswith("shared/dev/ceph-"))) | unique | select(length > 0)
  | "\($es.metadata.namespace)/\($es.metadata.name) \(join(" "))"'
```

**Reading.**

- Four lines, each ending `shared/dev/ceph-csi`: `ceph-csi-cephfs-prd/csi-cephfs-secret`,
  `ceph-csi-cephfs-prd/csi-cephfs-secret-user`, `ceph-csi-rbd-prd/csi-rbd-secret` and
  `ceph-csi-rbd-prd/csi-rbd-secret-user`. They are the four the dev `cephx` plan syncs
  (`DEV_READERS`, SecretRotator `kinds/cephx/ceph.py`).
- Another reader of `shared/dev/ceph-csi`: the dev plan would not sync it. Stop.
- Any reader of `shared/dev/ceph-rgw/s3`: the dev `rgw-admin` plan syncs nothing, and its
  `rgw_admin.delete` would cut that reader off. Stop.

**The dev write token's sync.** In the `iac` shell, one of the four synced as each `eso.sync … on
dev` step syncs it: with the dev write token the KubeCoder catalog holds. The Secret's content
stays as it is, since the leaf is unchanged:

```sh
check 'import datetime, types
from secret_rotator.cluster import Cluster, Ref
from secret_rotator.k8ssteps import EsoSync
from secret_rotator.kinds.k8s_sa_token.reach import Dev, connect
ctx = types.SimpleNamespace(bao=bao, now=datetime.datetime.now(datetime.UTC), progress=print)
es = Ref("ceph-csi-rbd-prd", "csi-rbd-secret-user")
print(es, EsoSync(Cluster(Dev(connect).kube(bao)), es).run(ctx))'
```

**Reading.**

- Perhaps a `waiting for ESO to sync it` line, then `ceph-csi-rbd-prd/csi-rbd-secret-user synced,
  Ready`.
- `HTTP 403` on the patch: dev's `edit` does not let `kubecoder-rw` patch ExternalSecrets. A dev
  `cephx` plan would fail at its first dev sync, after its `kv.write`, and roll back. Stop.
- `did not sync within …`: its last answer is ESO's message on dev. Read it.

**Dev's Ceph checks.** In the `iac` shell, run the Ceph checks again on dev, from
[the guest agent](#the-ceph-checks-from-srviac) to the self-removal:

```sh
export C=dev
```

**Reading:** as on prd, with one VM, `srvk8sdev`, and one RGW instance, `http://192.168.188.17`.
`client.k8s` lists `srvk8sdev` while dev's pods mount Ceph volumes. The import replaces the key the
converge gave `client.k8s-b`.

**The shutdown**, as the executor sends it after a dev plan that started the VM, `sudo -n qm
shutdown 919 --timeout 300 --forceStop 1`:

```sh
check 'from secret_rotator.vmsteps import Pve
pve = Pve()
pve.shutdown(pve.find("srvk8sdev"))
print(pve.find("srvk8sdev"))'
```

**Reading:** within 5 min, `Guest(name='srvk8sdev', node='pve', vmid=919, status='stopped')`. A
traceback from `qm shutdown 919 on pve`: a dev plan would leave srvk8sdev running after the night,
its line in the night's console reading `srvk8sdev is not shut down: …`. Read it, then shut dev down
by hand.

`exit` leaves the shell.

### Switching the Ceph kinds on

Each kind goes into `kinds_enabled` in a commit of its own, in either order (item 14 of
[§ Going live](#going-live)). Before each commit, read the kind's two plans again as
[the annotations](#the-ceph-kinds-annotations) give them. A Ceph key has no stamp, so a kind's first
night rotates both its leaves, prd's and dev's. The dev plan starts srvk8sdev, and the console then
reads `srvk8sdev shut down on pve` under it. Each kind's first live plans prove what the checks
could not: the whole plan, in the order it runs.

**`rgw-admin`**, once the RGW checks and the self-removal read as they should on both clusters. Its
first night, on each cluster:

- The mint adds a key to `k8s`: `✓ add a new S3 key to the RGW admin user k8s on prd · added key
  <id> to k8s`.
- `kv.write` writes the new pair. On prd, `argocd-hooks/argocd-hook-credentials` syncs.
- The silent proof has every instance take the new key.
- The delete removes the key the leaf held: `✓ remove the key the leaf held · removed key <old id>
  of k8s`.

The hook takes the new pair at its next Job run. A shell that sourced `scripts/setup-env.sh` before
the rotation holds the removed key: source it again.

How a plan fails:

- A refused mint fails the plan before its `kv.write`, and nothing changes.
- A failed proof or delete rolls back: the leaf holds the old key again, re-synced, and the mint's
  undo removes the added key.
- A delete RGW refuses fails the undo in the same way, which the self-removal check rules out. Both
  keys then stay valid, and the leaf holds the old one. Remove the added key by hand, as there, its
  id from the mint's line.

**`cephx`**, once the guest agent, the sessions, the import, dev's readers and the dev sync read as
they should. Its first night, on each cluster:

- The mint finds no client on `client.k8s-b`, then gives it a new key and `client.k8s`'s caps: `✓
  give the idle Ceph client on prd a new key · client.k8s-b re-keyed with client.k8s's caps`.
- `kv.write` moves the leaf to `client.k8s-b`.
- The syncs run: on prd the four CSI Secrets and the hook's, on dev dev's four.
- The silent proof authenticates as `client.k8s-b`.

The plan ends no key. Volumes mounted before it keep `client.k8s` and its old key. New mounts and
the hook use `client.k8s-b` at once:

```sh
bao kv get -mount=kv -field=user_id shared/prd/ceph-csi </dev/null
```

**Reading:** `k8s-b`. The user name is not a secret.

**The second rotation, by hand.** `client.k8s`'s key on prd came from the migration whose
transcript exposed it, and the first rotation leaves it valid for every volume mounted before. The
second rotation re-keys `client.k8s`, once no client uses it. Mounts move to `client.k8s-b` as
their pods restart. All of them move at the next node update round that reboots the nodes:
`IaC/Scheduled Update` runs weekly, and drains and reboots a node only when its update requires it.
After such a round, run [the monitors' sessions](#the-ceph-checks-from-srviac) check again with
`C=prd`.

**Reading:** `client.k8s: [] []`, and `client.k8s-b:` listing the k8s nodes. A node still listed
for `client.k8s`: restart the Ceph-backed pods on it, or wait for the next round that reboots it.

Then run the plan on srviac, and answer `y` to `Start it?`. It restarts nothing:

```sh
srviac 'secret-rotator run shared/prd/ceph-csi'
```

**Reading:** the mint reads `client.k8s re-keyed with client.k8s-b's caps`, every step is done, and
`user_id` reads `k8s` again. The exposed key is gone. Too early, the mint refuses before any change:
`the monitors list Ceph clients of client.k8s, which the plan would re-key, on <nodes>: restart the
Ceph-backed pods there, or let the next update round that reboots them move them`. Abort the plan:
it changed nothing, and nothing rolls back.

Source `scripts/setup-env.sh` again in any shell that sourced it before the first rotation. Its
`HOMELAB_CEPH_*` hold `client.k8s` and the exposed key, which Ceph refuses after the second
rotation.

Dev's `client.k8s` keeps its key until dev's next rotation. To retire it sooner, run `srviac
'secret-rotator run shared/dev/ceph-csi'` with srvk8sdev off, after dev's first rotation. Dev then
boots with every Secret on `client.k8s-b`, so its check finds no client on `client.k8s`.

A rotation blocked later, on any night, is in [`openbao.md`](openbao.md) §5.

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

      `kubeconfig` and `kubeconfig-dev-write` need dev. Each plan's `vm.start` starts srvk8sdev
      through `pve` if it is off and waits up to 15 min for dev's apiserver to answer. Once the
      plan ends, the run shuts srvk8sdev down again if the plan started it. If the apiserver does
      not answer in time, the run skips the plan the quiet way: no rotation, no rollback, no
      Telegram, srvk8sdev shut down again. The card lists it, `srvk8sdev did not answer within 15
      min: dev does not answer at https://10.1.3.3:16443: GET /version: transport error: …`, and
      it is due again the next night.

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
      (design R112). A refusal fails it at its mint, before its `kv.write`. The plans found dev
      running, so they leave it running: shut it down after, with `ssh root@pve qm shutdown 919`.

12. **Wave 3**, once [Wave 3's annotations](#wave-3s-annotations) are applied. Its kinds go in
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

13. **Wave 4**, once [§ Wave 4](#wave-4)'s token, annotations and check are done. `terraform` goes
    in alone, in one commit. Before it, read [wave 4's plans](#wave-4s-plans) and
    [its check](#wave-4s-check-from-srviac) again, then stagger the markers' first nights.

    **The stagger** (slice 048's ruling F5). A marker without a stamp is due at once, so on the
    kind's first night all ten would rotate, and again every 14 days: ten commits, and five apps
    restarted in one night. Stamp each marker instead as if it had rotated 14 days before its own
    first night, one night apart, in the order of `markers`: the five Postgres roles, the three S3
    keys, then the two backup tokens, `keycloak-dev` two nights before `keycloak-prd`. A rotation
    stamps the night it runs, so the 14-day interval keeps them a night apart. `first` is the
    kind's first night: the night after the commit, whose build and image rebuild finish that day.
    Stamp on the day of the commit, before it, with `markers` as [wave 4's plans](#wave-4s-plans)
    set it:

    ```sh
    first=<YYYY-MM-DD>  # the date of the kind's first night
    i=0; for m in $markers; do
      srviac "secret-rotator stamp rotator/terraform/${m%#*} ${m#*#} --rotated-at $(date -d "$first $((i - 14)) days" +%F)"
      i=$((i + 1)); done
    for m in $markers; do srviac "secret-rotator plan rotator/terraform/${m%#*}" | grep 'plan of'; done
    ```

    **Hand back:** the full output.

    **Reading.**

    - Ten `rotator/terraform/<leaf>#<key>: rotation stamp <date>, was none` lines, the first dated
      14 days before `first` and each next one a day later. `--rotated-at` takes no date after
      today, so `first` is at most 5 days out. An `error:` line wrote nothing for that marker: wave
      4's annotations create its leaf and key.
    - Ten `terraform plan of <key> · due <date>` lines, from `first` to 9 days after it, in the same
      order.

    Then commit `terraform` into `kinds_enabled`. The morning after `first`, the night's console
    names that commit in `secret-rotator run, commit <sha>`, and `terraform` among the kinds of the
    line after it. A night that ran an earlier commit ran without the kind: stamp again before the
    next night, with that night as `first`. The lines then read `was <date>`.

    What its nights do:

    - One marker a night, then each one 14 days after its rotation. A plan that fails before its
      commit is rolled back and due again the next night, beside that night's own marker.
    - The commit, `rotate <keeper> (secret-rotator)`, lands on the deploy repo's `main`. Its push
      starts the Application's auto-sync, whose PreSync hook re-mints the credential. Where no sync
      of the commit has started within a minute, the `argocd.sync` step requests one itself, of the
      head of `main`. It waits at most 15 minutes for the sync.
    - From the hook's re-mint until the restart is Ready, the app's new connections to its database
      fail, and for an S3 key its requests (slice 048's ruling D1). No rotation has timed that
      window yet: the readings below do.
    - The S3 reader and the backup tokens restart nothing: the CronJob that reads each Secret takes
      the new credential at its next run. A backup token's scope holds no token between the hook's
      `DELETE` and its `PUT`. A `PUT` that fails errors `credential "<scope>" was deleted but not
      re-created`, and the sync fails with it.

    The morning after each night, the night's console shows the plan `rotated`, and a consumer that
    did not come back is on the card. `tfwatch` reads the night's rotation. Its arguments are the
    Application, the keeper, the repo, the stage, the credential's resource in the hook's
    Terraform, and the workload the plan restarts. Run its line for the night's marker:

    ```sh
    job="$JENKINS_URL/job/IaC/job/Scheduled%20Secret%20Rotation"
    curl -fsS -u "$JENKINS_USER:$JENKINS_TOKEN" "$job/lastBuild/consoleText" | grep -A 20 'terraform plan of'
    tfwatch() { local app=$1 keeper=$2 repo=$3 stage=$4 resource=$5 restart=$6
      bao kv get -mount=kv -field=token rotator/terraform/credentials </dev/null | sed 's/^/Authorization: Bearer /' \
        | curl -fsS -H @- "https://api.github.com/repos/pvginkel/$repo/commits?path=config/$stage/rotation.tfvars&per_page=1" \
        | jq -r '.[0] | "\(.sha[:7]) \(.commit.committer.date) \(.commit.message)"'
      k -n argocd-prd get application "$app" -o jsonpath='{.status.sync.revision} {.status.sync.status} {.status.health.status}{"\n"}'
      k -n argocd-hooks get job "tf-presync-$app" -o jsonpath='{.status.completionTime}{"\n"}'
      k -n argocd-hooks logs "job/tf-presync-$app" | sed 's/\x1b\[[0-9;]*m//g' \
        | grep -E "tfvars file|rotation\.tfvars|$resource|Apply complete|No changes"
      [ -z "$restart" ] || { k -n "$app" get "$restart" -o jsonpath='{.spec.template.metadata.annotations.kubectl\.kubernetes\.io/restartedAt}{"\n"}'
        k -n "$app" get pods -o custom-columns='NAME:.metadata.name,READY:.status.conditions[?(@.type=="Ready")].lastTransitionTime'; }
      srviac "secret-rotator plan rotator/terraform/$app/$keeper" | grep 'plan of'; }
    tfwatch electronics-inventory-prd db ElectronicsInventoryDeploy prd module.db.random_password.this deployment/electronics-inventory
    tfwatch iot-prd db IotDeploy prd module.db.random_password.this deployment/iotsupport
    tfwatch keycloak-dev db KeycloakDeploy dev module.db.random_password.this deployment/keycloak
    tfwatch guacamole-prd db GuacamoleDeploy prd module.db.random_password.this deployment/guacamole
    tfwatch keycloak-prd db KeycloakDeploy prd module.db.random_password.this deployment/keycloak
    tfwatch electronics-inventory-prd s3 ElectronicsInventoryDeploy prd module.s3.homelab_s3_storage.this deployment/electronics-inventory
    tfwatch iot-prd s3 IotDeploy prd module.s3.homelab_s3_storage.this deployment/iotsupport
    tfwatch storage-prd backup_reader StorageDeploy prd homelab_s3_reader.backup_reader
    tfwatch youtrack-prd backups YoutrackDeploy prd homelab_backup_credential.backups
    tfwatch postgres-pas-prd backups PostgresPasDeploy prd homelab_backup_credential.backups
    ```

    **Hand back:** the full output.

    **Reading.**

    - The console: `terraform plan of rotator/terraform/<app>/<keeper> (<key>)`, then these `✓`
      lines, then `rotated`:
      - `commit a new <keeper> keeper to pvginkel/<Repo> · <sha7>: <keeper> = <UTC time> in
        config/<stage>/rotation.tfvars`;
      - `sync Argo CD Application <app> · <sha7> synced by auto-sync: Synced`, or `Synced, Healthy`
        where nothing restarts. `by secret-rotator` is a sync the step requested itself, and the
        first one is the proof that Argo CD takes the step's request;
      - `prove Terraform re-minted Secret <app>/<Secret> · Secret <app>/<Secret> changed`;
      - the `kv.write` of the marker, then one `roll out <app>/deployment/<name> · Ready,
        Application <app> Healthy` per restart.
    - The newest commit to the keeper file: the console's `<sha7>`, its time, then
      `rotate <keeper> (secret-rotator)`. Another commit names another writer of the file: read it.
    - The Application: the commit or a later head of `main`, then `Synced Healthy`.
    - The hook Job's completion time, then its log:
      - `with <n> tfvars file(s)`, and the `terraform … apply` line with the `-var-file` of
        `config/<stage>/rotation.tfvars`;
      - the resource's `Refreshing state...` line, its plan line, `# <resource> must be replaced`
        for a `random_password` or `# <resource> will be updated in-place` for the provider's
        resources, then its apply lines;
      - `Apply complete! Resources: …`. `No changes.` is a sync whose Terraform re-minted nothing,
        and the plan's proof then failed.

      The Job holds its latest run alone: a sync after the night's, an image-pin commit's,
      replaces it. Read the night's run then in Kibana or through the API
      ([`argocd.md`](argocd.md#a-replaced-hooks-log-kibana-or-the-api)).
    - Where the plan restarts: the workload's `restartedAt`, the rotator's restart, after the hook
      Job's completion time, then the namespace's pods, each with its Ready time. The new pod's
      Ready time less the hook Job's completion time is about D1's window: hand it back.
    - `terraform plan of <key> · due <date>`, 14 days after the night: the stamp.

    The S3 reader's and the backup tokens' consumers prove the new credential at their CronJob's
    next run after the rotation. Read each then:

    ```sh
    for c in storage-prd/s3-mirror youtrack-prd/youtrack-backup postgres-pas-prd/postgres-backup; do
      k -n "${c%/*}" get cronjob "${c#*/}" -o jsonpath='{.metadata.name} {.status.lastScheduleTime} {.status.lastSuccessfulTime}{"\n"}'; done
    ```

    **Reading:** for the night's marker, its CronJob's `lastSuccessfulTime` after its
    `lastScheduleTime`, both after the rotation. A `lastSuccessfulTime` before its
    `lastScheduleTime` is a run that failed: read its Job's log.

    A plan that fails:

    - Before its commit, its Telegram line ends `The run rolls it back; the leaf is due again.`
      Nothing changed. `pvginkel/<Repo> has no config/<stage>/rotation.tfvars on main` can also
      mean the token lacks the repository ([wave 4's check](#wave-4s-check-from-srviac)).
    - After its commit, its line says that it is not rolled back and stays stopped for
      `secret-rotator run <leaf>`. A keeper mints a new credential, so nothing undoes the commit.
      The nightly run leaves the plan alone: `not started: the leaf has its terraform plan in
      flight, on the card`. Find the cause by the failed step:
      - `the sync of <app> at <sha7> ended Failed: …`, or no sync within 15 minutes: the hook's
        log, as above, and [`argocd.md`](argocd.md). A backup token's `was deleted but not
        re-created` is mended by the next sync, which creates the token again.
      - `Secret <app>/<Secret> did not change: the sync of <sha7> took <keeper> = <value> in
        pvginkel/<Repo> config/<stage>/rotation.tfvars, and its Terraform re-minted nothing`: the
        repo's `terraform/main.tf` on `main` does not pass `lookup(var.rotation_epoch, "<keeper>",
        null)` to the credential's resource.
      - A `roll out` that did not come Ready: the workload's pods and events.

      Then `srviac 'secret-rotator run rotator/terraform/<app>/<keeper>'` takes the plan up where it
      stopped. Where it stopped before its proof passed, the proof commits a fresh keeper and waits
      for that commit's sync before the plan can stamp: the credential is re-minted once more.

14. **The Ceph kinds**, once [§ The Ceph kinds](#the-ceph-kinds)'s annotations are applied and its
    checks, prd's and dev's, read as they should. `rgw-admin` and `cephx` go in one per commit, in
    either order, as [Switching the Ceph kinds on](#switching-the-ceph-kinds-on) gives each. After
    `cephx`'s first night comes its second, hand-run prd rotation, once a node update round has
    rebooted the nodes.

**The stops.** The immediate stop is disabling the job. `enable` in place of `disable` reverses it:

```sh
curl -fsS -u "$JENKINS_USER:$JENKINS_TOKEN" -X POST "$JENKINS_URL/job/IaC/job/Scheduled%20Secret%20Rotation/disable"
```

`paused: true`, committed, stops every run before it does anything, once its build has rebuilt the
image. A paused night still pushes its run health, with `secret_rotator_paused` 1, so
`SecretRotatorStale` stays quiet. A disabled job pushes nothing, and `SecretRotatorStale` fires
once its last run is 48 h old.
