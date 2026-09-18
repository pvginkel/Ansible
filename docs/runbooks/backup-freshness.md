# Backup freshness runbook

Triage for the two backup alerts, and retiring a backup that is not coming back. Read this when
`BackupOverdue` or `BackupWatcherBlind` fires, when a `backup-server` scope is retired on purpose,
or when a database is dropped from `postgres-pas`.

Design context: "OpenBao backup / DR" and §"Backup" in
[`../../../AnsibleSpecs/decisions.md`](../../../AnsibleSpecs/decisions.md). `backup-server` is
DockerImages `backup-server/`, run as the `backup-server` Deployment in `storage-prd` (HelmCharts
`charts/storage`). Both rules are in the `backup-freshness` group of HelmCharts
`configs/prd/prometheus/prd/values.yaml`. Restoring OpenBao from its backup is
[`openbao.md`](openbao.md) §3.

## Conventions

- Every command here is the operator's keystroke. Claude prepares and explains; it does not run
  them.
- Several `kubectl` commands exec into pods in `storage-prd` and `prometheus-prd`, so their `prd`
  context must be allowed to.
- The rclone commands run inside the `backup-server` pod, on its own Drive login. No rclone host
  or Drive login of your own is needed.
- Prometheus answers on the LAN at `http://prometheus.home`, without a login. A helper prints each
  series of an instant query:

  ```bash
  q() { curl -s http://prometheus.home/api/v1/query --data-urlencode "query=$1" | jq -c '.data.result[] | [.metric, .value[1]]'; }
  ```

## How the watching works

- An upload to `backup-server` may declare how long it stays valid: `valid_for`, a Go duration such
  as `52h`, in its query string. `backup-server` stores the backup as
  `gdrive-pieter:Homelab Backups/<scope>/<YYYYMMDDTHHMMSSZ>_<filename>.age`, the stamp being the
  upload's time in UTC. Once the backup has landed it writes `<that name>.metadata.json` beside it,
  holding `{"valid_for":"52h"}`. An upload that declares nothing gets no metadata file.
- A **stream** is a scope plus a file name. It is watched while any of its kept backups has a
  metadata file. It is valid until its newest backup's stamp plus the validity its newest
  declaring backup declared. An uploader that stops declaring therefore stays watched.
- Every folder directly under `Homelab Backups` is a scope and is read, whether or not the scope
  still has a credential. Removing a credential does not stop the watching; §3 does.
- `backup-server` reads every scope folder at start and then hourly: a **full read**. It also
  re-reads a scope right after each upload to it. Of the metadata files it reads only each
  stream's newest, and only when it does not remember it already.
  When a read fails, `backup-server` keeps publishing what it read last.
- Pruning keeps each scope's newest `retention` backups (set per scope by
  `homelab_backup_credential`) and deletes a pruned backup's metadata file with it. A stream whose
  backups are all pruned stops being watched, and its alert clears 6 h later.
- The metrics are on pod port 8081, path `/metrics`, inside the cluster only. Per stream:
  `backup_server_stream_valid_until_timestamp_seconds{scope,filename}` and
  `backup_server_stream_last_backup_timestamp_seconds{scope,filename}`. Once:
  `backup_server_refresh_last_success_timestamp_seconds`, the end of the last full read that read
  every folder without error, `0` before the first.

Streams watched today:

| Scope | Stream (`filename`) | Uploader | Runs | Validity | Kept |
|---|---|---|---|---|---|
| `openbao` | `openbao-backup.tgz` | `openbao-backup.timer` on each `srvvaultN`; only the Raft leader uploads | 02:00, plus up to 1 h random delay | 52 h | 14 |
| `postgres-pas` | `<db>.dump`, one per database but `postgres` and `app` | `postgres-backup` CronJob in `postgres-pas-prd` | 02:00 | 52 h | 90 across all databases, about ten nights |

With 52 h, one missed night stays quiet and two in a row alert. The `youtrack` scope's uploads
declare nothing, so it is not watched; `YouTrackBackupStale` covers it
([`youtrack-restore.md`](youtrack-restore.md)).

What Prometheus sees, what `backup-server` serves, and what is on Drive:

```bash
q 'sort((backup_server_stream_valid_until_timestamp_seconds - time()) / 3600)'   # hours left per stream; negative is overdue
q 'up{service="backup-server"}'                                                    # the scrape target
q '(time() - backup_server_refresh_last_success_timestamp_seconds) / 3600'         # hours since the last full read
kubectl --context prd -n storage-prd exec deploy/backup-server -- curl -s localhost:8081/metrics
kubectl --context prd -n storage-prd exec deploy/backup-server -- rclone lsf 'gdrive-pieter:Homelab Backups/<scope>/' | sort
```

## 1 — `BackupOverdue` fires

The labels `scope` and `filename` name the stream. Its newest backup is older than the validity it
declared, so for a 52 h stream two nightly uploads in a row did not land. The backups already on
Drive stay restorable. The rule has no grace of its own, and all overdue streams arrive in one
Telegram group.

1. **If `BackupWatcherBlind` fires too, start at §2.** A `backup-server` that cannot read Drive
   keeps publishing what it read last, so a stream can look overdue while its backups land.

2. **Find the newest backup on Drive.** The last line is the newest that landed:

   ```bash
   kubectl --context prd -n storage-prd exec deploy/backup-server -- \
     rclone lsf 'gdrive-pieter:Homelab Backups/<scope>/' --include '/????????T??????Z_<filename>.age' | sort | tail -n 3
   ```

3. **Read the uploader's log.**
   - **`openbao`** — every node's timer runs, but only the Raft leader uploads; the others log
     `not the Raft leader; nothing to do` and exit successfully. On each node, from `srviac` once
     ufw is on:

     ```bash
     ssh srvvaultN systemctl list-timers openbao-backup.timer
     ssh srvvaultN sudo journalctl -u openbao-backup -n 20 --no-pager
     ```

     A failed run names the call that broke; [`openbao.md`](openbao.md) "What can go wrong" has the
     fixes. Once fixed, start the unit on every node; the followers exit at once:

     ```bash
     for n in 1 2 3; do ssh srvvault$n sudo systemctl start openbao-backup.service; done
     ```

   - **`postgres-pas`** — the newest Job's log:

     ```bash
     kubectl --context prd -n postgres-pas-prd get jobs
     kubectl --context prd -n postgres-pas-prd logs job/<name>
     ```

     `ERROR: pg_dump failed for <db>` and `ERROR: upload failed for <db>: HTTP …` name a database
     that missed; the closing `FAILED databases: …` lists them all, and the others were uploaded. A
     log that stops without `all databases backed up` means the Job was killed. Once fixed, run it
     now:

     ```bash
     kubectl --context prd -n postgres-pas-prd create job --from=cronjob/postgres-backup postgres-backup-manual
     ```

     A database dropped on purpose stays overdue until pruning removes its last dump. §3 retires
     it sooner.

   - **Any other scope** — that uploader's own log.

4. **An upload `backup-server` answered with an error** — the uploader logs the status:
   - `401` — the upload token is wrong or revoked. OpenBao's is rotated per
     [`openbao.md`](openbao.md) §5. `postgres-pas`'s is the `postgres-backup-upload` Secret, which
     the release's Terraform writes.
   - `400` — `backup-server` rejected the `filename` or the `valid_for`.
   - `413` — the body is over `backup-server`'s size limit.
   - `500` — `backup-server` could not encrypt or store it. Its log says why, on an
     `upload failed scope=<scope>` or `metadata write failed scope=<scope>` line:

     ```bash
     kubectl --context prd -n storage-prd logs deploy/backup-server --since 72h | grep 'scope=<scope>'
     ```

5. **Cleared** — at the first scrape after a backup lands: `backup-server` re-reads the scope right
   after the upload. The stream's hours left (first view above) are back near 52.

## 2 — `BackupWatcherBlind` fires

No scrape of `backup-server` in the last 6 h reported a successful full read in the last 6 h. Either
Prometheus cannot scrape it (its pod is down, crash-looping or gone), or it cannot read Drive. While
this fires, `BackupOverdue` cannot be trusted to. A healthy full read is at most 1 h 10 m old, so
the 6 h passes a redeploy and three failed hourly reads.

1. **Is it running and scraped?**

   ```bash
   kubectl --context prd -n storage-prd get pods -l app=backup-server
   q 'up{service="backup-server"}'
   ```

   - No pod, one not ready, or restarts climbing — `kubectl describe` the pod and read
     `kubectl logs --previous`.
   - `up` returns nothing — Prometheus has no target: the `backup-server` Service lost its
     `prometheus.io/*` annotations or its endpoints (HelmCharts
     `charts/storage/templates/backup-server-service.yaml`).
   - `up` is `0` — the target is there but its scrape fails. The `curl … localhost:8081/metrics`
     view under "How the watching works" shows whether the pod serves the metrics at all.

2. **Are its reads failing?** The full read logs `freshness refresh: <error>`, one line per failed
   scope; the re-read after an upload logs `freshness refresh scope=<scope>: <error>`:

   ```bash
   kubectl --context prd -n storage-prd logs deploy/backup-server --since 12h | grep -A3 'freshness refresh'
   ```

   - `list gdrive-pieter:Homelab Backups: …` — Drive cannot be listed at all. The uploads use the
     same login, so they fail with `HTTP 500` too. The login is the `gdrive-pieter` remote in
     `/data/rclone.conf` on the `rclone-backup-pvc` volume; renewing it is not covered here.
   - `scope <scope>: list: …` or `scope <scope>: read …` — one scope's folder, or one metadata file
     in it, cannot be read. The other scopes still update, but no full read succeeds until it can.
   - `scope <scope>: <path>: …` about the file's content — a metadata file `backup-server` cannot
     parse, such as one edited by hand. Delete that one file (§3 step 3, its exact name as the
     `--include`); its backup stays and declares nothing.
   - `signal: killed` or `context deadline exceeded` in any of these lines — a read ran past its
     10-minute limit: Drive answered too slowly, it did not refuse. The rclone call running at the
     limit logs the first; one started after it logs the second.

3. **Cleared** — at the first scrape that reports a successful full read. A restart reads at once.
   Run it outside the nightly uploads (01:30–03:00; YouTrack's starts at 01:30): an upload fails
   while the pod restarts.

   ```bash
   kubectl --context prd -n storage-prd rollout restart deploy/backup-server
   ```

## 3 — Retire a scope or a stream

A scope retired on purpose, its uploader gone and its `homelab_backup_credential` perhaps removed,
stays watched: the watching reads Drive, not the credential store. Its streams alert critical once
their validity runs out, and keep alerting until their metadata files are deleted by hand. The same
holds for one stream, such as a database dropped from `postgres-pas`, except that pruning ends it
after about ten nights. The backups themselves may stay.

1. **Make sure nothing uploads to it any more.** An upload that declares a validity makes the
   stream watched again.

2. **List its metadata files.** For a whole scope:

   ```bash
   kubectl --context prd -n storage-prd exec deploy/backup-server -- \
     rclone lsf 'gdrive-pieter:Homelab Backups/<scope>/' --include '*.metadata.json'
   ```

   For one stream, use `--include '/????????T??????Z_<filename>.age.metadata.json'` instead. The
   stamp's pattern keeps `db.dump` from matching `other_db.dump` too.

3. **Delete them**, dry run first, with the same `--include`:

   ```bash
   kubectl --context prd -n storage-prd exec deploy/backup-server -- \
     rclone delete 'gdrive-pieter:Homelab Backups/<scope>/' --include '*.metadata.json' --dry-run
   kubectl --context prd -n storage-prd exec deploy/backup-server -- \
     rclone delete 'gdrive-pieter:Homelab Backups/<scope>/' --include '*.metadata.json'
   ```

4. **Silence `BackupOverdue` for 8 h**, whether or not it fires yet. After the deletion the alert
   keeps firing, or starts, for about 6–7 h: the streams drop out on the next full read, within the
   hour, and the rule's 6 h look-back holds their last values after that. For one stream, add
   `filename=<filename>`:

   ```bash
   kubectl --context prd -n prometheus-prd exec prometheus-prd-alertmanager-0 -c alertmanager -- \
     amtool --alertmanager.url=http://localhost:9093 silence add \
     --duration=8h --comment='<scope> retired' alertname=BackupOverdue scope=<scope>
   ```

5. **Confirm**, after the next full read, that the scope publishes no stream: this prints nothing.

   ```bash
   q 'backup_server_stream_valid_until_timestamp_seconds{scope="<scope>"}'
   ```
