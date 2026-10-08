# SecretRotator go-live (slice 045)

This runbook brings SecretRotator up on srviac. First part: its credentials, its annotations and
its nightly job, which runs in dry run. Then a week of dry run. Then going live, one kind at a time.
[§ Wave 1](#wave-1) prepares the kinds of slice 047, before the go-live or after it.

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
it is applied once, by hand:

```sh
k apply -f /work/SecretRotator/k8s/cluster-identity.yaml
k -n kube-system get secret secret-rotator-token -o jsonpath='{.data.token}' | base64 -d | wc -c
k -n kube-system get secret secret-rotator-token -o jsonpath='{.data.token}' | base64 -d | bao kv put -mount=kv iac/rotator-k8s-token token=-
```

**Hand back:** the full output.

**Reading.**

- `serviceaccount/secret-rotator`, `clusterrolebinding.rbac.authorization.k8s.io/secret-rotator-admin`
  and `secret/secret-rotator-token`, each `created`.
- The token's length in bytes, not `0`.
- The `kv put` answers with `version 1`.

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
YouTrack scope, on Jeeves's Account Security page. Store it:

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
- `set     max_versions=20  (was 0)` on the automatic leaves, the six `rotator/approle/*` markers
  among them. The `rotator/bootstrap/*` markers are `manual` and keep the mount's default.
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

Then push JenkinsDeploy's `main`, which holds the change. Once Argo CD has synced it:

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

Then push InfraStatisticsDeploy's `main`, which holds the change. Its sync changes the
Deployment's pod template, so Argo CD restarts infra-statistics. The new pod starts once ESO has
written the token into the Secret. Once it has synced:

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

## Going live

Each switch change is a commit to SecretRotator's `main`, in `src/secret_rotator/switches.yaml`. It
takes effect once its build (`IaC/SecretRotator`), green at its lint and tests, has reset `prd` and
`IaC/IaC Docker Image` has rebuilt the image. The next run's first line names the commit it runs.

1. **`dry_run: false`**, with `kinds_enabled: [random]` and `max_rotations_per_run: 10` as
   committed. From the next night on, the run rotates at most 10 due `random` plans a night, so the
   first pass drains over the nights after. From that night's push on, `SecretRotationFailed` and
   `SecretRotationOverdue` can fire too ([`openbao.md`](openbao.md) §5).
2. **`manual` next**, in the commit after `random`'s first clean night. It executes nothing, since a
   plan with an operator step never runs at night. What it turns on is the manual-due status, the
   Telegram lines and the card lines for the manual rotations, which are all due at go-live because
   none has a stamp.
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

**The stops.** The immediate stop is disabling the job. `enable` in place of `disable` reverses it:

```sh
curl -fsS -u "$JENKINS_USER:$JENKINS_TOKEN" -X POST "$JENKINS_URL/job/IaC/job/Scheduled%20Secret%20Rotation/disable"
```

`paused: true`, committed, stops every run before it does anything, once its build has rebuilt the
image. A paused night still pushes its run health, with `secret_rotator_paused` 1, so
`SecretRotatorStale` stays quiet. A disabled job pushes nothing, and `SecretRotatorStale` fires
once its last run is 48 h old.
