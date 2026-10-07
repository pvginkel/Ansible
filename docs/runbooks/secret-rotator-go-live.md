# SecretRotator go-live (slice 045)

This runbook brings SecretRotator up on srviac. First part: its credentials, its annotations and
its nightly job, which runs in dry run. Then a week of dry run. Then going live, one kind at a time.

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
- `IaC/SecretRotator` builds the push green, resets `prd` to it, and starts
  `IaC/IaC Docker Image`, which is green too. Until that image is built, a run has no chat id and
  posts nothing to Telegram.

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

Then share the `Secret Rotator` tag with Jeeves, in the tag's settings in YouTrack, so that Jeeves
sees it and can add it to an issue. The run finds the open card, and tags a new one, only through
tags its token sees. Check what the token sees:

```sh
yt() { bao kv get -mount=kv -field=token rotator/youtrack </dev/null | sed 's/^/Authorization: Bearer /' | curl -sS -H @- "https://issues.webathome.org/api/$1"; }
yt 'users/me?fields=login' | jq -r .login
yt 'tags?fields=name&$top=500' | jq -r '.[].name' | grep -x 'Secret Rotator'
yt 'admin/projects?fields=shortName&$top=500' | jq -r '.[].shortName' | grep -x ANS
```

**Hand back:** the full output.

**Reading.**

- Each `kv put` answers with `version 1`.
- Jenkins reads the admin's user id, then `true`.
- YouTrack reads Jeeves's login, then `Secret Rotator`, then `ANS`. A missing tag line means the
  tag is not shared with Jeeves yet. With it missing, every run fails with
  `YouTrack shows its token no tag Secret Rotator`.

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
  leaves; 0 unchanged, 0 absent from the store, 0 live leaf(s) not in the seed`. That is every leaf
  of the seed, since none holds an entry yet.
- An `absent from the store` line names a seed leaf the store lacks. When it is one of the leaves
  of steps 1 to 5, finish that step first.
- A `not in the seed` line names a leaf the seed does not cover yet. The apply leaves it whole, old
  keys included, and the audit reports it until the seed covers it
  ([`openbao.md`](openbao.md#a-new-leaf)).
- A `no kind in the seed, no entry: <leaf>#<key>` or `named in the seed, not held by the leaf:
  <leaf>#<key>` line names a key on which the seed and the leaf disagree. The audit then reports
  that key, so fix the seed first.
- No `cannot write:` line. With one, the dry run ends `nothing written` and exits 1.

```sh
srviac 'secret-rotator annotate --apply'
srviac 'secret-rotator annotate' | tail -n 1
```

**Hand back:** the full output.

**Reading.**

- The apply lists the dry run's writes again, then prints `patching 123 leaf(s), 12 of them new
  marker leaves; …`. Then it prints one `patched <leaf>` or `created and patched <leaf>` line per
  leaf, and exits 0.
- The dry run after it reads `would patch (dry run; --apply writes) 0 leaf(s), 0 of them new
  marker leaves; 123 unchanged, …`.
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
```

**Hand back:** the full output, the build's console, the standing card and the Telegram message.

**Reading.**

- `SUCCESS`.
- `secret-rotator run, commit <sha>` names the commit `prd` held, then
  `secret-rotator run, <date> (dry run): kinds random, at most 10 rotation(s)`.
- The trigger's spec reads `30 5 * * *`.
- An open ANS card tagged `Secret Rotator` exists, marked as a dry run, and Homelab Alerts has the
  rotator's digest, marked as a dry run.

## The dry-run week

The job runs every night at 05:30 in dry run. Each morning, read the night's console, the card and
the digest. They show the plans the run would have executed, each with its steps, and the manual
rotations that are due. A finding on the card is fixed in the seed or in the store
([`openbao.md`](openbao.md) §5). Go live after a week whose nights raised nothing unexplained.

## Going live

Each switch change is a commit to SecretRotator's `main`, in `src/secret_rotator/switches.yaml`. It
takes effect once its green build (`IaC/SecretRotator`) has reset `prd` and `IaC/IaC Docker Image`
has rebuilt the image. The next run's first line names the commit it runs.

1. **`dry_run: false`**, with `kinds_enabled: [random]` and `max_rotations_per_run: 10` as
   committed. From the next night on, the run rotates at most 10 due `random` plans a night, so the
   first pass drains over the nights after.
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

**The stops.** The immediate stop is disabling the job. `enable` in place of `disable` reverses it:

```sh
curl -fsS -u "$JENKINS_USER:$JENKINS_TOKEN" -X POST "$JENKINS_URL/job/IaC/job/Scheduled%20Secret%20Rotation/disable"
```

`paused: true`, committed, stops every run before it does anything, once its build has rebuilt the
image.
