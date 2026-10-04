# OpenBao hygiene cutover (slice 044)

This cutover puts the `kv` mount in the state that the rotator (slice 045) expects, and takes the
Elasticsearch superuser password out of git. In order, it:

- converges the `openbao` role;
- pushes the three held repos and rolls their consumers;
- moves and mints the Elasticsearch credentials, then rotates the superuser;
- deletes the orphan leaves and the two composite leaves;
- destroys the stale AppRole secret_id accessors;
- annotates every leaf and checks the annotations, last.

The operator runs every step, from top to bottom. Each step gives the commands, what to hand back,
and the reading that must hold before the next step starts.

Context:

- Slice 044's [`plan.md`](../../../AnsibleSpecs/slices/completed/044_openbao_hygiene_before_rotation/plan.md)
  holds the rulings behind every step (S1–S13, D1, B1–B3, A2).
- [`secret-rotation/design.md`](../../../AnsibleSpecs/secret-rotation/design.md) and
  [`catalog.md`](../../../AnsibleSpecs/secret-rotation/catalog.md).
- [`scripts/rotation/README.md`](../../scripts/rotation/README.md) describes the tools.

Facts are as of 2026-10-04.

## Conventions

- **The operator's keystroke.** That covers every OpenBao write and delete, every Elasticsearch API
  call, every push, every `kubectl` write and every playbook run. The session that accompanies the
  operator reads the full output of each step and confirms the step's reading before the next one.
  Hand back the full output of every command block.
- **No secret on a screen, on a command line or in a file.** Moved values travel in a pipe: the
  superuser password out of git, and the Kibana encryption key out of `kibana-config#file`. New
  values are generated inside the pipe that writes them. Every check compares or authenticates
  without printing.
  - The one exception is S8's `jenkins` fallback in step 12. `accessor_cleanup.py` shows a fresh
    secret_id on the terminal, and the operator pastes it into the Jenkins credential.
  - The only leaf values this file prints are `username` keys, which the catalog classes `none`.
- **A pipe source never ends a pipeline.** `leaf`, `gitpw` and `newpw` below print a secret. Run
  them only with a pipe after them.
- **Shell.** Set this up in each bash shell that runs these steps, and again in any new shell. A
  step's own variables (`pushed`, `job`, `rolled`, `prev`, `gone`, `roles`) live only in the shell
  that set them, so finish each step in one shell. Re-source `bao-login.sh` once `bao` answers
  `permission denied`, which means its token has expired.

  ```sh
  cd /work/Ansible && . scripts/bao-login.sh
  bao() { cexec iac bao "$@"; }
  k()   { cexec iac kubectl --kubeconfig "$HOME/.kube/config-prd-write" --context prd "$@" </dev/null; }
  now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
  ES=http://elasticsearch.elasticsearch-prd.svc.cluster.local:9200
  EDP=/work/scratch/ElasticsearchDeploy IDP=/work/scratch/IntercomDeploy DIM=/work/DockerImages

  # Pipe sources: each prints a secret on stdout.
  leaf()  { bao kv get -mount=kv ${3:+-version=$3} -field="$2" "$1" </dev/null; }   # leaf PATH KEY [VERSION]
  gitpw() { git -C "$EDP" show 377472a:config/prd/values.yaml \
              | python3 -c 'import sys, yaml; sys.stdout.write(yaml.safe_load(sys.stdin)["elasticsearch"]["password"])'; }
  newpw() { openssl rand -hex 24 | tr -d '\n'; }

  # Pipe sinks: each reads a secret on stdin and prints none.
  basic()  { python3 -c 'import base64, sys; print("Authorization: Basic " + base64.b64encode(f"{sys.argv[1]}:{sys.stdin.read()}".encode()).decode())' "$1"; }
  esauth() { basic "$1" | curl -sS -o /dev/null -w "$1 %{http_code}\n" -H @- "$ES/_security/_authenticate"; }
  asjson() { python3 -c 'import json, sys; json.dump({sys.argv[1]: sys.stdin.read()}, sys.stdout)' "$1"; }
  # One line each: copied from this indented block, a multi-line python3 -c carries the indent
  # and fails with IndentationError.
  kibanayml() { python3 -c 'import json, sys, yaml; flat = lambda d, p="": {fk: fv for k, v in d.items() for fk, fv in (flat(v, f"{p}{k}.") if isinstance(v, dict) else {f"{p}{k}": v}).items()}; json.dump(flat(yaml.safe_load(sys.stdin)), sys.stdout)'; }
  same() { python3 -c 'import json, sys; a, b = (json.load(open(f)) for f in sys.argv[1:3]); [print(k, "equal" if k in a and k in b and a[k] == b[k] else "DIFFERS") for k in sorted(a.keys() | b.keys())]; sys.exit(a != b)' "$@"; }

  # es CURL-ARGS...: curl as elastic, with the current version of the elastic leaf (from step 4 on).
  es() { curl -sS -H @<(leaf eso/prd/elasticsearch/prd/elastic password | basic elastic) "$@"; echo; }

  # esstate NS NAME [SINCE]: Ready, refreshed at or after SINCE, synced on its current spec.
  esstate() { k -n "$1" get externalsecret "$2" -o json | jq -r --arg t "${3:-}" '.metadata.generation as $g
    | "\(.metadata.namespace)/\(.metadata.name) ready=\([.status.conditions[]? | select(.type == "Ready") | .status][0] // "Unknown") refreshed=\((.status.refreshTime // "") >= $t) on-generation=\((.status.syncedResourceVersion // "") | startswith("\($g)-"))"'; }
  # esync NS NAME: forces a sync and waits up to two minutes for it.
  esync() { local t s; t=$(now); k -n "$1" annotate externalsecret "$2" force-sync="$(date +%s)" --overwrite >/dev/null
    for _ in $(seq 24); do sleep 5; s=$(esstate "$1" "$2" "$t")
      [[ $s == *"ready=True refreshed=true on-generation=true" ]] && break; done; echo "$s"; }
  ```

  `same` prints each key with `equal` or `DIFFERS`, and exits 1 on any difference. `kibanayml`
  flattens nested settings into dotted keys, the way Kibana reads `kibana.yml`, so neither
  quoting nor layout affects the compare.
- **ESO** refreshes every hour, so a step that needs a value live forces the sync with `esync`. When
  a sync fails, ESO keeps the previous Secret. A compare against an ExternalSecret's Secret proves
  something only once `esstate` reads `ready=True refreshed=true on-generation=true`.
- **Pods read their env and their `subPath` mounts at start.** Nothing restarts a pod when its
  Secret changes, and filebeat is a DaemonSet. Each step rolls what it needs.

## Facts

**The held commits.** Each one is the local `main` of its clone, ahead of `origin/main`.

| Repo | Clone | Held commit | What a push to `main` does |
| --- | --- | --- | --- |
| DockerImages | `/work/DockerImages` | `9e458e4` (P4) | The Jenkins job `DockerImages` builds `elasticsearch-setup` and commits its pin into ElasticsearchDeploy's `main`, which Argo deploys. |
| ElasticsearchDeploy | `/work/scratch/ElasticsearchDeploy` | `be1f93d` (P5) | Argo `elasticsearch-prd` syncs Elasticsearch (`Recreate`), Kibana and a new setup Job. |
| IntercomDeploy | `/work/scratch/IntercomDeploy` | `0135aa6` (P3) | Argo `intercom-prd` syncs the ExternalSecret `intercom-mcp-tokens`. The Deployment is unchanged. |

Charts already serves `homelab-shared` 0.5.0. The Applications `elasticsearch-prd`,
`filebeat-prd`, `intercom-prd` and `iot-prd` live in `argocd-prd`. Each syncs automatically, with
prune and without self-heal.

**The Elasticsearch leaves after the cutover.**

| Leaf | Keys | Value | Secret (namespace): reader |
| --- | --- | --- | --- |
| `eso/prd/elasticsearch/prd/elastic` | `password` | the superuser's, moved from git, then rotated in step 10 | `elasticsearch-elastic` (elasticsearch-prd): Elasticsearch env and probes, setup Job |
| `eso/prd/elasticsearch/prd/kibana-system` | `password` | fresh | `elasticsearch-kibana-system`: Kibana env, setup Job |
| `eso/prd/elasticsearch/prd/kibana-encryption-key` | `key` | Kibana's existing key, moved | `elasticsearch-kibana-config`, `file` rendered by a template: Kibana's `kibana.yml` |
| `eso/prd/filebeat/prd/elastic-credentials` | `username` = `filebeat_writer`, `password` | fresh | `filebeat-es-credentials` (filebeat-prd): DaemonSet `filebeat`; `elasticsearch-filebeat-writer`: setup Job |
| `eso/prd/iot/prd/elastic-credentials` | `username` = `iotsupport`, `password` | fresh | `iot-elastic-credentials` (iot-prd): Deployment `iotsupport`, CronJob `iotsupport-rotation-cronjob`; `elasticsearch-iotsupport`: setup Job |
| `eso/prd/elasticsearch/prd/filebeat-reader` | `password` | unchanged | `elasticsearch-reader`: setup Job (user `reader`) |

**Elasticsearch.**

- It runs one node, version 8.15.0.
- The superuser password committed in ElasticsearchDeploy (`377472a:config/prd/values.yaml`,
  `elasticsearch.password`) is the live one today. `kibana_system` and the vestigial
  `logstash_internal` hold the same value.
- `ELASTIC_PASSWORD` only bootstraps the superuser (Elastic docs, untested here). Once the API sets
  the password, every holder of the old value is stale. That includes the Elasticsearch probes,
  which authenticate with `$ELASTIC_PASSWORD` and then fail.
- The setup Job authenticates as `elastic` and retries every refused request forever. It runs only
  when its spec changes, which also renames it (`elasticsearch-setup-<hash>`). A changed Secret
  value never runs it.
- The new users' privileges come from Elastic's docs, not from a test. A pod stays green while its
  writes are refused. IoTSupport ignores errors on single items of a `_bulk` response and logs only
  a refused request as a whole. Step 8 therefore proves the switch by new documents, not by a
  quiet log.

## The sequence

1. **Pre-flight**, read-only.
2. **The `openbao` converge**, check first (D1, B1).
3. **IntercomDeploy**: push it, compare its rendered Secret, roll intercom.
4. **The three Elasticsearch leaves**: two values moved in, one minted.
5. **DockerImages**: push it. The pin lands, and the new setup Job fails at start.
6. **The consumer leaves and the ElasticsearchDeploy push**, as one step.
7. **Kibana**: compare its config, then roll it.
8. **filebeat and iot onto their own users.**
9. **`logstash_internal` and `logstash_writer`**: delete them.
10. **The superuser rotation**, as one step.
11. **The orphans and the two composites**: delete them.
12. **The stale secret_id accessors**: destroy them.
13. **The annotations**: apply them.
14. **The check**, last.

Two follow-ups come after the cutover: [the next UTC day's iot index](#afterwards-the-next-utc-day)
and [`eso-dev`](#afterwards-eso-dev-when-the-dev-cluster-is-next-up), the next time the dev cluster
is up.

The order rests on these constraints:

- A leaf exists before any push whose ExternalSecret reads it: step 4 comes before step 6.
- Ruling B3 orders the user switch. The filebeat and iot leaves take their new users right before
  the ElasticsearchDeploy push that first runs the new setup Job (step 6). filebeat and iot roll
  onto those users only once that Job has completed (step 8). A filebeat or iot pod that restarts
  between the two comes up as a user that does not exist yet, so step 6 runs without pauses.
- filebeat and iot run on their own users before the superuser password changes: step 8 comes
  before step 10.
- The superuser's API change, its leaf, the forced sync and the Elasticsearch roll are one step
  (step 10). The setup Job takes the new value from the same Secret on its next run.
- DockerImages is pushed before ElasticsearchDeploy (step 5 before step 6). The held
  ElasticsearchDeploy commit is rebased onto the pin that the DockerImages build commits. Until it
  lands, the new setup image runs against the old chart's env, and it fails at start without
  calling Elasticsearch.
- The composites and orphans are deleted, and the converge grants `patch`, before the annotation
  apply: steps 11 and 2 come before step 13. The check runs last, on a store that holds only live
  leaves (step 14).

## 1 — Pre-flight

`es` needs the `elastic` leaf, which step 4 writes. Until then, the pre-flight authenticates with
`gitpw`.

```sh
for r in $DIM $EDP $IDP; do git -C $r fetch -q && git -C $r status -sb | head -1 && git -C $r log --oneline origin/main..main; done
k -n argocd-prd get applications elasticsearch-prd filebeat-prd intercom-prd iot-prd
k -n elasticsearch-prd get pods,jobs
curl -sS -H @<(gitpw | basic elastic) "$ES/_security/user" | jq -r 'keys[]'
for u in elastic kibana_system logstash_internal; do gitpw | esauth $u; done
for l in elastic kibana-system kibana-encryption-key; do bao kv metadata get -mount=kv eso/prd/elasticsearch/prd/$l </dev/null 2>&1 | head -1; done
scripts/rotation/annotate.py | grep -E '^(absent|not in the seed|would patch)'
```

**Hand back:** the full output. Note the Elasticsearch and Kibana pod names, because step 5 must
leave both pods as they are.

**Reading.**

- Each repo reads `[ahead N]` and lists only slice 044's commits: the held commit of
  [Facts](#facts), plus any later slice 044 commit. A repo that is also `behind` gets
  `git -C <clone> rebase origin/main` before its step.
- All four Applications are `Synced` and `Healthy`.
- The user list holds `logstash_internal`, and holds neither `filebeat_writer` nor `iotsupport`.
- The logins read `elastic 200`, `kibana_system 200` and `logstash_internal 200`: the git value is
  live for all three. Steps 6, 9 and 10 end that.
- Each of the three new leaves reads `No value found at kv/metadata/…`.
- The annotation dry run lists the three new leaves as `absent from the store, skipped`.
  - Its `not in the seed:` lines name exactly the 13 orphans and the two composites of step 11.
  - If `jenkins/keycloak-da-admin` has already been deleted, that leaf is a fourth `absent` line.

## 2 — The `openbao` converge (D1, B1)

The role now declares each AppRole's `secret_id_ttl`, at `0` (never) on all six roles. It also
grants `openbao-admin` `patch` on the KV mount, which step 13 needs. Check mode comes first:

```sh
cd /work/Ansible/ansible && cexec iac poetry run ansible-playbook playbooks/site-openbao.yml --check
```

**Hand back:** the full output.

**Reading.**

- `Report the AppRole writes a real run would perform (check mode)` skips all six items: the store
  holds `secret_id_ttl` 0 on every role, as declared, so no AppRole would be rewritten (V08).
- `Report the policy writes a real run would perform (check mode)` reports one item,
  `openbao-admin`: the `patch` grant. `Write consumer policies (only when text differs)` shows as
  skipped, because `uri` has no check mode.
- Any other changed task, or another policy in that report, is drift that this slice did not
  bring: stop and read it.

Then the real run, which is the same command without `--check`, and the proof:

```sh
cd /work/Ansible/ansible && cexec iac poetry run ansible-playbook playbooks/site-openbao.yml
cd /work/Ansible && bao policy read openbao-admin | grep -A2 'path "kv/\*"'
```

**Hand back:** the full output.

**Reading.**

- Exactly one item changed: `Write consumer policies (only when text differs)` for
  `openbao-admin`.
- `Write AppRoles (only when settings differ)` runs no item.
- The policy's `kv/*` stanza lists `"patch"`, which is B1's proof.

## 3 — IntercomDeploy: push, compare, roll

The held commit renders `intercom-mcp-tokens` from the bearer leaves of the two MCP servers. Both
leaves exist already. Key `trello` renders as `Bearer <token>` from
`eso/prd/trello-mcp/prd/trello#bearer-token`, and key `jenkins` from
`eso/prd/jenkins-mcp/prd/config#bearer-token`. The push leaves the Deployment as it is, so the
running pod keeps its env until the roll below, and the compare runs before that roll.

```sh
pushed=$(now); git -C $IDP push origin main && track_build.py --deploy IntercomDeploy main "$(git -C $IDP rev-parse HEAD)"
esstate intercom-prd intercom-mcp-tokens "$pushed"
same <(k -n intercom-prd get secret intercom-mcp-tokens -o json | jq '.data | map_values(@base64d)') \
     <(bao kv get -mount=kv -format=json eso/prd/intercom/prd/mcp </dev/null | jq .data.data)
```

**Hand back:** the full output.

**Reading.**

- track_build reports `intercom-prd` rolled.
- The ExternalSecret reads `intercom-prd/intercom-mcp-tokens ready=True refreshed=true on-generation=true`.
- The compare prints `jenkins equal` and `trello equal`. This is what proves the trello header's
  `Bearer` form, which until now was inferred.

**Stop** on anything else. Nothing has restarted, so intercom still runs on its old env. If the
compare differs, revert the push and resync:

```sh
git -C $IDP revert --no-edit HEAD && git -C $IDP push origin main && esync intercom-prd intercom-mcp-tokens
```

Once the compare is equal, roll intercom:

```sh
k -n intercom-prd rollout restart deploy/intercom && k -n intercom-prd rollout status deploy/intercom --timeout=5m
```

**Reading:** `successfully rolled out`. The leaf `eso/prd/intercom/prd/mcp` stays until step 11.

## 4 — The three Elasticsearch leaves

Nothing reads these leaves until step 6. The superuser password moves in from git, and the Kibana
encryption key moves in from `kibana-config#file`, so Kibana's sessions and saved objects survive.
`kibana_system` gets a new password.

```sh
gitpw | bao kv put -mount=kv eso/prd/elasticsearch/prd/elastic password=-
leaf eso/prd/elasticsearch/prd/kibana-config file \
  | python3 -c 'import sys, yaml; v = yaml.safe_load(sys.stdin)["xpack.security.encryptionKey"]; assert isinstance(v, str), "not a string"; sys.stdout.write(v)' \
  | bao kv put -mount=kv eso/prd/elasticsearch/prd/kibana-encryption-key key=-
newpw | bao kv put -mount=kv eso/prd/elasticsearch/prd/kibana-system password=-
```

The proof:

```sh
leaf eso/prd/elasticsearch/prd/elastic password | esauth elastic
same <(leaf eso/prd/elasticsearch/prd/kibana-encryption-key key | asjson key) \
     <(leaf eso/prd/elasticsearch/prd/kibana-config file | python3 -c 'import json, sys, yaml; json.dump({"key": yaml.safe_load(sys.stdin)["xpack.security.encryptionKey"]}, sys.stdout)')
leaf eso/prd/elasticsearch/prd/kibana-system password | wc -c
es "$ES/_security/_authenticate" | jq -r .username
```

**Hand back:** the full output.

**Reading.**

- Each of the three `bao kv put` answers shows version 1.
- The rest reads `elastic 200`, `key equal`, `48` and `elastic`.
- A failed pipe source leaves an empty value in its leaf, which these four lines catch. If that
  happens, rerun the line that wrote that leaf.

## 5 — DockerImages: push it

```sh
git -C $DIM push origin main && track_build.py --hash "$(git -C $DIM rev-parse HEAD)" --no-follow-argocd DockerImages
git -C $EDP fetch -q && git -C $EDP log --oneline -1 origin/main
k -n argocd-prd get application elasticsearch-prd -o jsonpath='{.status.sync.revision} {.status.sync.status}{"\n"}'
```

Wait until Argo shows that commit as `Synced`. Argo picks up a push within minutes. Then read the
new Job:

```sh
job=$(k -n elasticsearch-prd get jobs -o name --sort-by=.metadata.creationTimestamp | tail -1); echo "$job"
k -n elasticsearch-prd logs "$job"; k -n elasticsearch-prd describe "$job" | tail -n 5; k -n elasticsearch-prd get pods
```

**Hand back:** the full output, including the pin's build number `<n>`.

**Reading.**

- The build is green and built `elasticsearch-setup` alone.
- ElasticsearchDeploy's `origin/main` is `ci: image pins from DockerImages #<n> (elasticsearch-setup)`.
- The newest Job logs `Missing required input(s): FILEBEAT_WRITER_PASSWORD, IOTSUPPORT_PASSWORD`.
  It retries with backoff and ends Failed, with `BackoffLimitExceeded` in its events.
- Argo reports `elasticsearch-prd` Degraded until step 6. This is the expected outcome, and it is
  why this track_build runs with `--no-follow-argocd`.
- The Elasticsearch and Kibana pods have the same names and ages as in the pre-flight.

## 6 — The consumer leaves and the ElasticsearchDeploy push

Run this step as one, without pauses. From the first leaf write until the setup Job completes,
ESO's hourly refresh can reach a filebeat or iot pod that restarts, and that pod comes up as a user
that does not exist yet.

The rebase comes first, so that a conflict stops the step before any leaf changes. The same block
records the two consumer leaves' versions, which are the way back:

```sh
git -C $EDP rebase origin/main && git -C $EDP log --oneline -3 && grep -n elasticsearchSetup $EDP/config/prd/values.yaml
for l in filebeat iot; do bao kv metadata get -mount=kv -format=json eso/prd/$l/prd/elastic-credentials </dev/null | jq -r --arg l $l '"\($l) version \(.data.current_version)"'; done
```

**Hand back:** the full output, including the two versions.

**Reading.**

- The held commit (`chart: no password in values; every credential from OpenBao …`) sits on
  step 5's pin commit, which sits on `377472a`.
- `elasticsearchSetup: ':<n>'` names step 5's build.

Then write the two leaves and push:

```sh
newpw | bao kv put -mount=kv eso/prd/filebeat/prd/elastic-credentials username=filebeat_writer password=- \
  && newpw | bao kv put -mount=kv eso/prd/iot/prd/elastic-credentials username=iotsupport password=- \
  && pushed=$(now) && git -C $EDP push origin main
track_build.py --deploy ElasticsearchDeploy main "$(git -C $EDP rev-parse HEAD)" --roll-timeout 1500
```

The sync does three things:

- It rolls Elasticsearch (`Recreate`) on the git value, which `…/elastic` now delivers.
- It rolls Kibana on `…/kibana-system`. `kibana_system` does not have that password until the Job
  sets it, so Kibana is refused until then. Kibana retries, and its startup probe allows 25
  minutes.
- It runs the new setup Job.

Read every line below, whatever track_build reports:

```sh
for n in elastic kibana-system kibana-config filebeat-writer iotsupport; do esstate elasticsearch-prd elasticsearch-$n "$pushed"; done
job=$(k -n elasticsearch-prd get jobs -o name --sort-by=.metadata.creationTimestamp | tail -1); echo "$job"
k -n elasticsearch-prd wait --for=condition=complete "$job" --timeout=15m && k -n elasticsearch-prd logs "$job"
k -n elasticsearch-prd rollout status deploy/elasticsearch --timeout=10m
leaf eso/prd/filebeat/prd/elastic-credentials password | esauth filebeat_writer
leaf eso/prd/iot/prd/elastic-credentials password | esauth iotsupport
leaf eso/prd/elasticsearch/prd/kibana-system password | esauth kibana_system
gitpw | esauth kibana_system
```

**Hand back:** the full output.

**Reading.**

- track_build reports `elasticsearch-prd` rolled. If Kibana is its only unhealthy resource once the
  Job has completed, go on: step 7 rolls Kibana.
- All five ExternalSecrets read `ready=True refreshed=true on-generation=true`.
- The Job wait reads `condition met`. The Job's log then runs in this order:
  1. `Waiting for Elasticsearch availability`
  2. `Setting kibana_system password`
  3. `Creating filebeat_writer role and user`
  4. `Creating iotsupport role and user`
  5. `Creating reader user`
  6. `Configuring logstash-http ILM policy and index template`
  7. `All done!`

  `Error: …; retrying` lines right after the first line are Elasticsearch still starting. After
  any later line, such a line is a request that Elasticsearch refuses.
- `deployment "elasticsearch" successfully rolled out`.
- The logins read `filebeat_writer 200`, `iotsupport 200`, `kibana_system 200` and
  `kibana_system 401`.

**Stop** if the Job has not completed within 15 minutes. Its log names the request it keeps
retrying. filebeat and iot still run as `elastic` on the git value, which Elasticsearch still
accepts, so do not roll them. To take the two leaves back, use the versions handed back above:

```sh
bao kv rollback -mount=kv -version=<filebeat version> eso/prd/filebeat/prd/elastic-credentials
bao kv rollback -mount=kv -version=<iot version> eso/prd/iot/prd/elastic-credentials
esync filebeat-prd filebeat-es-credentials; esync iot-prd iot-elastic-credentials
```

## 7 — Kibana: compare its config, then roll it

In step 6, `esstate` showed `elasticsearch-kibana-config` synced on its template. Its Secret is
therefore the rendering, not the copy of the leaf.

```sh
same <(k -n elasticsearch-prd get secret elasticsearch-kibana-config -o json | jq -r .data.file | base64 -d | kibanayml) \
     <(leaf eso/prd/elasticsearch/prd/kibana-config file | kibanayml)
```

**Hand back:** the full output.

**Reading:** six `equal` lines and exit status 0. The keys are `elasticsearch.hosts`,
`elasticsearch.password` (the literal `${ELASTICSEARCH_PASSWORD}` on both sides),
`elasticsearch.username`, `server.host`, `server.publicBaseUrl` and `xpack.security.encryptionKey`.

**Stop** on any `DIFFERS`. If the differing key is `xpack.security.encryptionKey`, step 4's move
went wrong. Rerun step 4's second pipeline, run `esync elasticsearch-prd elasticsearch-kibana-config`,
and compare again.

Once the compare is equal, roll Kibana:

```sh
k -n elasticsearch-prd rollout restart deploy/kibana && k -n elasticsearch-prd rollout status deploy/kibana --timeout=25m
curl -sS -o /dev/null -w 'kibana %{http_code}\n' http://kibana.home/api/status
```

**Reading:** `successfully rolled out` and `kibana 200`. The new pod mounted the rendered file and
reached Elasticsearch as `kibana_system` with its own password. `/api/status` answers 503 while it
cannot.

## 8 — filebeat and iot onto their own users

First prove that both Secrets carry the new user:

```sh
esync filebeat-prd filebeat-es-credentials; esync iot-prd iot-elastic-credentials
k -n filebeat-prd get secret filebeat-es-credentials -o jsonpath='{.data.username}' | base64 -d; echo
k -n iot-prd get secret iot-elastic-credentials -o jsonpath='{.data.username}' | base64 -d; echo
k -n filebeat-prd get secret filebeat-es-credentials -o jsonpath='{.data.password}' | base64 -d | esauth filebeat_writer
k -n iot-prd get secret iot-elastic-credentials -o jsonpath='{.data.password}' | base64 -d | esauth iotsupport
```

**Reading:** both ExternalSecrets read `ready=True refreshed=true on-generation=true`. The usernames
are `filebeat_writer` and `iotsupport`. The logins read `filebeat_writer 200` and `iotsupport 200`.

Then roll both consumers:

```sh
k -n filebeat-prd rollout restart ds/filebeat && k -n filebeat-prd rollout status ds/filebeat --timeout=10m
k -n iot-prd rollout restart deploy/iotsupport && k -n iot-prd rollout status deploy/iotsupport --timeout=10m
rolled=$(now); echo "$rolled"
```

The CronJob `iotsupport-rotation-cronjob` takes the new Secret at its next run.

Ten minutes later, take the proof (A2). Documents timestamped after `rolled` can only come from the
new pods, because every old pod is gone by then.

```sh
for p in 'filebeat-*' 'logstash-http-*'; do printf '%s ' "$p"; es -H 'Content-Type: application/json' "$ES/$p/_count?filter_path=count" -d "{\"query\":{\"range\":{\"@timestamp\":{\"gte\":\"$rolled\"}}}}"; done
k -n filebeat-prd logs -l app=filebeat --since=15m --tail=-1 | grep -Ec 'security_exception|40[13] (Unauthorized|Forbidden)'
k -n iot-prd logs deploy/iotsupport -c iotsupport-app --since=15m | grep -Ec 'security_exception|HTTP error 40[13]'
k -n iot-prd get secret iot-elastic-credentials -o jsonpath='{.data.password}' | base64 -d | basic iotsupport \
  | curl -sS -H @- "$ES/logstash-http-*/_count?filter_path=count"; echo
```

**Hand back:** the full output.

**Reading.**

- Both counts are above 0.
- Both log greps print `0`.
- The last line prints a count: `iotsupport` may read `logstash-http-*`.
- If `logstash-http-*` stays at 0, read it again later. It counts only what devices post.

**Stop** on a 401 or 403. It means a new user's privileges fall short, and the fix is a
DockerImages change. Until that fix lands, put the consumer back on `elastic`: step 6's rollback,
then roll it again. That works only because the superuser has not been rotated yet.

## 9 — `logstash_internal` and `logstash_writer`

Logstash runs nowhere, and `logstash_internal` holds the git value.

```sh
es -X DELETE "$ES/_security/user/logstash_internal"
es -X DELETE "$ES/_security/role/logstash_writer"
es -o /dev/null -w '%{http_code}\n' "$ES/_security/user/logstash_internal"
es -o /dev/null -w '%{http_code}\n' "$ES/_security/role/logstash_writer"
```

**Hand back:** the full output.

**Reading:** `{"found":true}` twice, then `404` twice (V18).

## 10 — The superuser rotation

This is one step, with four parts:

1. The leaf takes a new version.
2. The API sets that version as the password, authenticating with the previous version.
3. The Secret syncs.
4. Elasticsearch rolls onto the new value.

```sh
prev=$(bao kv metadata get -mount=kv -format=json eso/prd/elasticsearch/prd/elastic </dev/null | jq .data.current_version); echo "prev=$prev"
leaf eso/prd/elasticsearch/prd/elastic password "$prev" | esauth elastic
newpw | bao kv put -mount=kv eso/prd/elasticsearch/prd/elastic password=-
leaf eso/prd/elasticsearch/prd/elastic password \
  | python3 -c 'import json, sys; sys.stdout.write(json.dumps({"password": sys.stdin.read()}))' \
  | curl -sS -w '\nHTTP %{http_code}\n' -H @<(leaf eso/prd/elasticsearch/prd/elastic password "$prev" | basic elastic) \
         -H 'Content-Type: application/json' --data-binary @- "$ES/_security/user/elastic/_password"
```

**Reading:** `prev=1` (higher if step 4 wrote the leaf more than once), `elastic 200`, the put
answering version `prev`+1, and then `{}` with `HTTP 200`.

**Stop** on anything but `HTTP 200`. Take the leaf back, which is the only change so far:

```sh
bao kv rollback -mount=kv -version="$prev" eso/prd/elasticsearch/prd/elastic
```

On `HTTP 200`, carry straight on:

```sh
esync elasticsearch-prd elasticsearch-elastic
k -n elasticsearch-prd rollout restart deploy/elasticsearch && k -n elasticsearch-prd rollout status deploy/elasticsearch --timeout=10m
leaf eso/prd/elasticsearch/prd/elastic password | esauth elastic
k -n elasticsearch-prd get secret elasticsearch-elastic -o jsonpath='{.data.password}' | base64 -d | esauth elastic
es "$ES/_security/user" | jq -r 'keys[]' | while read -r u; do gitpw | esauth "$u"; done
k -n elasticsearch-prd get pods
k -n filebeat-prd logs -l app=filebeat --since=10m --tail=-1 | grep -Ec 'security_exception|40[13] (Unauthorized|Forbidden)'
k -n iot-prd logs deploy/iotsupport -c iotsupport-app --since=10m | grep -Ec 'security_exception|HTTP error 40[13]'
```

**Hand back:** the full output.

**Reading.**

- The sync reads `ready=True refreshed=true on-generation=true`.
- Elasticsearch rolled out, which means its probes authenticate with the new value.
- `elastic 200`, twice.
- Then one line for every user the cluster lists, and each reads `<user> 401`. The git value
  authenticates as no user (V15, B3).
- The Elasticsearch and Kibana pods are Ready, and the setup Job reads `Completed`.
- Both log greps print `0`: Kibana, filebeat and iot reconnected after the restart.

## 11 — The orphans and the two composites

`kv metadata delete` removes every version of a leaf. After that, the only way back is a backup's
`kv.json`, which is kept for 14 days (S10). Steps 3 and 7 compared both composites equal to the
Secrets that replace them (S11).

```sh
gone=(
  eso/prd/ceph-csi-cephfs/prd/credentials eso/prd/ceph-csi-rbd/prd/credentials
  eso/prd/electronics-inventory/prd/db eso/prd/guacamole/prd/db eso/prd/iot/prd/db
  eso/prd/keycloak/prd/db eso/prd/media/prd/mydownloads-config
  eso/prd/open-webui/prd/db eso/prd/open-webui/prd/oidc eso/prd/pgadmin/prd/pgpass
  eso/prd/phpmyadmin/prd/webathome-org-config iac/jenkins-agent jenkins/elastic-test
  eso/prd/elasticsearch/prd/kibana-config eso/prd/intercom/prd/mcp
)
k get externalsecret -A -o json | jq -r '.items[] | (.spec.data[]?.remoteRef.key), (.spec.dataFrom[]? | .extract.key // empty)' \
  | sort -u | grep -xF -f <(printf '%s\n' "${gone[@]}")
(cd ansible && cexec iac poetry run ansible srviac -b -m shell -a 'grep -c "iac/jenkins-agent" /etc/iac/secrets.yaml || true')
```

**Hand back:** the full output.

**Reading.**

- The first command prints nothing: no prd ExternalSecret reads any of the 15 leaves. None of the
  15 is under `eso/dev`.
- The second reads `srviac | CHANGED | rc=0 >> 0`: srviac's `secrets.yaml` does not reference
  `iac/jenkins-agent`.
- Anything else: stop.

Then delete the 15 leaves and confirm they are gone:

```sh
for l in "${gone[@]}"; do bao kv metadata delete -mount=kv "$l" </dev/null; done
for l in "${gone[@]}"; do bao kv metadata get -mount=kv "$l" </dev/null 2>&1 | head -1; done
```

**Reading:** 15 `Success! Data deleted (if it existed) at: kv/metadata/…` lines, then 15
`No value found at kv/metadata/…` lines (V01, and the deletion half of V05).

## 12 — The stale secret_id accessors

`accessor_cleanup.py` proves, per AppRole, which secret_id each consumer holds, then destroys
every other accessor of that role (S8). It reads secret_ids, so the operator runs it; it never
prints one. `eso-dev` waits until the dev cluster is up
([below](#afterwards-eso-dev-when-the-dev-cluster-is-next-up)).

```sh
roles="--role openbao-admin --role iac-agent --role jenkins --role eso --role backup"
scripts/rotation/accessor_cleanup.py $roles
```

**Hand back:** the full output.

**Reading.**

- There are five `<role>: proven` blocks. Each `keep` line names the consumer that holds that
  accessor.
- The last line reads `would destroy N accessor(s); untouched: none`, and the exit status is 0.
- On 2026-10-04 the store held 5 accessors for eso, 5 for jenkins, 4 for iac-agent, 4 for
  openbao-admin and 1 for backup. The newest of each role dates from 2026-08-13, but the proof
  decides what is kept, not the date.
- `jenkins: untouched: …`, after a failed credential read, means the apply below needs
  `--jenkins-fallback`.
- Any other `untouched` line: stop. Its reason says what could not be read.

```sh
scripts/rotation/accessor_cleanup.py $roles --apply
```

**Reading:** the plan again, then one `destroyed <role> <accessor>` line per `destroy` line, and
exit status 0. A `stopped: …` line means OpenBao refused a destroy. The accessors already reported
destroyed are gone and the rest stay. Read the error.

**The jenkins fallback**, used only after a failed jenkins read, is
`scripts/rotation/accessor_cleanup.py $roles --apply --jenkins-fallback`:

1. Type `mint` when asked. The script mints one secret_id for `jenkins` and shows it on the
   terminal only.
2. Paste it into the Jenkins credential that the Vault plugin's global configuration names. That
   is `724520d1-a0c1-4fa3-8a9e-a027de7f469a`, the only Vault AppRole credential, in the system
   store.
3. Press Enter, which erases the value from the terminal.
4. Run the `AaC/Home Assistant Fleet` build from the block below.
5. Type `passed` once that build has passed.

Only then does the script destroy jenkins' other accessors.

**Every consumer still logs in:**

```sh
. scripts/bao-login.sh                                                                   # openbao-admin
esync intercom-prd intercom-mqtt; k get clustersecretstore openbao-prd                   # eso
(cd ansible && cexec iac poetry run ansible srviac -b -m command -a 'iac -c true')       # iac-agent
(cd ansible && cexec iac poetry run ansible openbao -b -m command -a 'systemctl start openbao-backup.service')   # backup
(cd ansible && cexec iac poetry run ansible openbao -b -m command -a 'journalctl -u openbao-backup -n 5 --no-pager')
n=$(curl -sS -u "$JENKINS_USER:$JENKINS_TOKEN" "$JENKINS_URL/job/AaC/job/Home%20Assistant%20Fleet/api/json?tree=nextBuildNumber" | jq .nextBuildNumber)
curl -sS -X POST -u "$JENKINS_USER:$JENKINS_TOKEN" -o /dev/null -w '%{http_code}\n' "$JENKINS_URL/job/AaC/job/Home%20Assistant%20Fleet/build"
track_build.py --buildnr "$n" --no-wait-downstream --no-follow-argocd 'AaC/Home Assistant Fleet'   # jenkins
scripts/rotation/accessor_cleanup.py $roles
```

**Hand back:** the full output.

**Reading.**

- openbao-admin: `bao-login: BAO_ADDR=… ttl=…`.
- eso: `ready=True refreshed=true on-generation=true`, and the store reads `Valid` and `True`. ESO
  logs in on every sync.
- iac-agent: `srviac | CHANGED | rc=0`. `iac-impl` logs in to resolve the `!bao` references of
  `secrets.yaml` before it runs any command.
- backup: on the Raft leader, the last `openbao-backup:` line reads
  `openbao-backup: backup uploaded (…)`; on the followers, `not the Raft leader; nothing to do`.
  systemd's own lines for the oneshot unit (`Deactivated successfully`, `Finished …`) follow it.
- jenkins: `201`, then a green build. Its `withVault` step reads `kv/jenkins/home-automation-fleet`.
- The final dry run shows five `proven` blocks and `would destroy 0 accessor(s); untouched: none`
  (V06).
- After the jenkins fallback, the credential read that sent you to it may fail again. The final dry
  run then shows four `proven` blocks, `jenkins: untouched: …` and `untouched: jenkins`, with exit
  status 1. That is expected: jenkins' proof is the fallback's green `withVault` build (V06). Do
  not follow the line's hint to mint another secret_id.
- A consumer that fails to log in has lost its secret_id. Re-mint it per [openbao.md](openbao.md)
  §5.

## 13 — The annotations

```sh
scripts/rotation/annotate.py
```

**Hand back:** the full output, which lists every leaf with the keys it would add or change.

**Reading.**

- The last line reads `would patch (dry run; --apply writes) 106 leaf(s); 0 unchanged, 0 absent
  from the store, 0 live leaf(s) not in the seed`.
- Once `jenkins/keycloak-da-admin` has been deleted, the count is 105, plus the line
  `absent from the store, skipped: jenkins/keycloak-da-admin`.
- A `not in the seed:` line names a leaf this cutover should have deleted: stop.

```sh
scripts/rotation/annotate.py --apply
scripts/rotation/annotate.py | tail -n 1
```

**Hand back:** the full output.

**Reading.**

- The apply prints `patching 106 leaf(s); …`, then one `patched <leaf>` line per leaf, and exits 0.
- The second dry run reads `would patch (dry run; --apply writes) 0 leaf(s); 106 unchanged, …`.
- `stopped at <leaf>: OpenBao refused the write … lacks the patch capability …` means step 2's
  grant is not on this token. Re-source `scripts/bao-login.sh`, read
  `bao policy read openbao-admin`, and run the apply again. It patches only what is left.

## 14 — The check, last

```sh
scripts/rotation/annotate.py --check; echo "exit $?"
```

**Hand back:** the full output.

**Reading:** `0 finding(s) on 0 of 106 leaf(s)` and `exit 0` (V10), or 105 once `jenkins/keycloak-da-admin` is deleted.
A finding line names a leaf and a key. The fix goes into the seed or the store; then run steps 13
and 14 again.

The cutover is done. `kv` holds only live leaves, every one annotated. The Elasticsearch superuser
password left git, and its value in git history authenticates as no user. ElasticsearchDeploy's
history is not rewritten (S13).

## Afterwards: the next UTC day

IoTSupport writes to one `logstash-http-<YYYY.MM.DD>` index per UTC day. Its first bulk write of the
day creates that index, using `iotsupport`'s `auto_configure`. A refused creation would go
unlogged, so read the index list once the cutover's first UTC midnight has passed:

```sh
es "$ES/_cat/indices/logstash-http-*?h=index,docs.count&s=index" | tail -n 3
```

**Reading:** the current UTC day's index is listed and holds documents.

## Afterwards: `eso-dev`, when the dev cluster is next up

The cutover does not start the dev cluster (S12). The next time it is up, run:

```sh
cd /work/Ansible && . scripts/bao-login.sh
scripts/rotation/accessor_cleanup.py --role eso-dev
scripts/rotation/accessor_cleanup.py --role eso-dev --apply
read -r ns name < <(cexec iac kubectl --context dev get externalsecret -A --no-headers </dev/null | awk 'NR == 1 {print $1, $2}')
cexec iac kubectl --kubeconfig ~/.kube/config-dev-write --context dev -n "$ns" annotate externalsecret "$name" force-sync="$(date +%s)" --overwrite </dev/null
sleep 10; cexec iac kubectl --context dev -n "$ns" get externalsecret "$name" </dev/null
```

The proof forces a sync of the first ExternalSecret the dev cluster lists.

**Reading.**

- `eso-dev: proven`, with a `keep` line naming the dev cluster's store, then its
  `destroyed eso-dev …` lines. The role held 2 accessors on 2026-10-04.
- The forced ExternalSecret reads `SecretSynced` and `True`, synced seconds ago. The dev cluster's
  ESO still logs in (V07).
