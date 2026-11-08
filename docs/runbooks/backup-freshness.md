# Backup freshness runbook

Triage for the two backup alerts, retiring a backup that is not coming back, and renewing a Drive
remote's login. Read this when `BackupOverdue` or `BackupWatcherBlind` fires, when a
`backup-server` scope is retired on purpose, when a database is dropped from `postgres-pas`, or
when a Google Drive remote in storage-prd's `rclone.conf` needs a new login.

Design context: "OpenBao backup / DR" and §"Backup" in
[`../../../AnsibleSpecs/decisions.md`](../../../AnsibleSpecs/decisions.md). `backup-server` is
DockerImages `backup-server/`, run as the `backup-server` Deployment in `storage-prd`
(HomelabAppsDeploy `storage/chart/`). Both rules are in the `backup-freshness` group of
PlatformAddOnsDeploy `prometheus/config/prd/values.yaml`, and its
`kc project test --project prometheus` unit-tests them
(`prometheus/tests/alert-rules/backup-freshness.yml`). Restoring OpenBao from its backup is
[`openbao.md`](openbao.md) §3.

## Conventions

- Every command here is the operator's keystroke. Claude prepares and explains; it does not run
  them.
- Several `kubectl` commands exec into pods in `storage-prd` and `prometheus-prd`, so their `prd`
  context must be allowed to.
- The rclone commands run inside the `backup-server` pod, on its own Drive login. No rclone host
  or Drive login of your own is needed, except for §4's new login, made on your own machine. They
  inherit its environment, which skips the Drive trash: a file `rclone delete` removes there (§3)
  is gone at once.
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
| `postgres-pas` | `<db>.dump`, one per database but `postgres` and `app` | `postgres-backup` CronJob in `postgres-pas-prd` | 01:00 | 52 h | 90 across all databases, about ten nights |
| `youtrack` | `youtrack.tar.gz` | `youtrack-backup` CronJob in `youtrack-prd` | 01:30 | 52 h | 30 |
| `claude-home` | `claude-home.tar.zst` | `claude-home-backup` CronJob in `kubecoder-prd` | 00:30 | 52 h | 3 |

With 52 h, one missed night stays quiet and two in a row alert.

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
Drive stay restorable. The rule has no grace of its own, and all overdue streams arrive as one
notification, `BackupOverdue ×<n>` when there are several.

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

   - **`youtrack`** — the newest Job's log, as for `postgres-pas` but in `youtrack-prd`.
     [`youtrack-restore.md`](youtrack-restore.md) "What can go wrong" reads its errors. Once fixed,
     run it now:

     ```bash
     kubectl --context prd -n youtrack-prd create job --from=cronjob/youtrack-backup youtrack-backup-manual
     ```

   - **`claude-home`** — the newest Job's log, as for `postgres-pas` but in `kubecoder-prd`.
     [`claude-home-restore.md`](claude-home-restore.md) "What can go wrong" reads its errors. Once
     fixed, run it now:

     ```bash
     kubectl --context prd -n kubecoder-prd create job --from=cronjob/claude-home-backup claude-home-backup-manual
     ```

   - **Any other scope** — that uploader's own log.

4. **An upload `backup-server` answered with an error** — the uploader logs the status:
   - `401` — the upload token is wrong or revoked. OpenBao's is rotated per
     [`openbao.md`](openbao.md) §5. `postgres-pas`'s is the `postgres-backup-upload` Secret,
     `youtrack`'s the `youtrack-backup-upload` Secret, and `claude-home`'s the
     `claude-home-backup-upload` Secret in `kubecoder-prd`; each release's Terraform writes its own.
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
     `prometheus.io/*` annotations or its endpoints (HomelabAppsDeploy
     `storage/chart/templates/backup-server-service.yaml`).
   - `up` is `0` — the target is there but its scrape fails. The `curl … localhost:8081/metrics`
     view under "How the watching works" shows whether the pod serves the metrics at all.

2. **Are its reads failing?** The full read logs `freshness refresh: <error>`, one line per failed
   scope; the re-read after an upload logs `freshness refresh scope=<scope>: <error>`:

   ```bash
   kubectl --context prd -n storage-prd logs deploy/backup-server --since 12h | grep -A3 'freshness refresh'
   ```

   - `list gdrive-pieter:Homelab Backups: …` — Drive cannot be listed at all. The uploads use the
     same login, so they fail with `HTTP 500` too. The login is the `gdrive-pieter` remote in
     `/data/rclone.conf` on the `rclone-backup-pvc` volume; §4 renews it.
   - `scope <scope>: list: …` or `scope <scope>: read …` — one scope's folder, or one metadata file
     in it, cannot be read. The other scopes still update, but no full read succeeds until it can.
   - `scope <scope>: <path>: …` about the file's content — a metadata file `backup-server` cannot
     parse, such as one edited by hand. Delete that one file (§3 step 3, its exact name as the
     `--include`); its backup stays and declares nothing.
   - `signal: killed` or `context deadline exceeded` in any of these lines — a read ran past its
     10-minute limit: Drive answered too slowly, it did not refuse. The rclone call running at the
     limit logs the first; one started after it logs the second.

3. **Cleared** — at the first scrape that reports a successful full read. A restart reads at once.
   Run it outside the nightly uploads, 00:30–03:00 (each one's start is in the table under "How
   the watching works"): an upload fails while the pod restarts.

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

## 4 — Renew a Drive remote's login

For a Google Drive remote in storage-prd's `rclone.conf` whose login no longer works, and for a
remote moving onto another OAuth client. The file is `/data/rclone.conf` on the
`rclone-backup-pvc` volume, root `0600`. Four things read it:

- `backup-server`, for its uploads and reads, on `gdrive-pieter`;
- the `s3-mirror` CronJob, 03:30, from its own copy of the file, on `gdrive-pieter`;
- `storage-sync-cronjob`, 01:10: an `rclone sync` of every remote in the file to ZFS, then a
  snapshot. Its Job completes even when a remote fails;
- `storage-refresh-keys-cronjob`, hourly at :00: `rclone about` on every remote, which renews each
  remote's token and writes it back.

Its Google Drive remotes:

| Remote | Google account | `scope` | Read by |
|---|---|---|---|
| `gdrive-pieter` | the operator's, which owns `Homelab Backups` | `drive` | all four |
| `gdrive-mariska` | Mariska's | `drive.readonly` | `storage-sync-cronjob`, `storage-refresh-keys-cronjob` |

Each logs in under the operator's own Google OAuth client, `rclone-backup`: a Desktop client in
the Google Cloud project `rclone-backup`, its consent screen External and In production. Its
client ID and secret are in Roboform, entry "Rclone backup Google Drive client", and in each
remote's section. `gdrive-mariska` still uses rclone's shared client, which is being retired,
until it moves (ANS-359). The file's two OneDrive remotes are not Google's; this section does not
renew them.

1. **Read what failed.** The hourly refresh logs one block per remote:

   ```bash
   kubectl --context prd -n storage-prd get jobs --sort-by=.metadata.creationTimestamp | grep refresh-keys
   kubectl --context prd -n storage-prd logs job/<newest storage-refresh-keys-cronjob-…> | grep -A6 'Refreshing keys for <remote>$'
   ```

   - `Total:`, `Used:` and the other quota lines — the login works.
   - `couldn't fetch token: invalid_grant` — the login is gone, most likely removed from the
     Google account's third-party connections. Renew it. Do not follow rclone's hint to run
     `rclone config reconnect`: that logs in on the host the remote is on, and the pod has no
     browser.
   - `invalid_client` — the client's secret was reset or the client deleted in Google's console.
     Every remote under it fails alike: renew each, with the client's current ID and secret.
   - `NOTICE: <remote>: This remote uses rclone's shared Google Drive client_id` — the remote
     has no client of its own yet. Move it with the steps below.

   For `gdrive-pieter`, §2 step 2's `freshness refresh` lines carry the same errors. A login that
   dies again a week after each renewal means the consent screen is back in Testing, where Google
   ends logins after 7 days: publish it again (Google Auth Platform, Audience).

2. **Log in on your machine**, with rclone and a browser, in bash. The client ID and secret come
   from Roboform. The scope is the remote's, from the table:

   ```bash
   read -rp 'client ID: ' RCLONE_DRIVE_CLIENT_ID
   read -rsp 'client secret: ' RCLONE_DRIVE_CLIENT_SECRET; echo
   export RCLONE_DRIVE_CLIENT_ID RCLONE_DRIVE_CLIENT_SECRET RCLONE_DRIVE_SCOPE=<the remote's scope>
   rclone authorize drive
   unset RCLONE_DRIVE_CLIENT_ID RCLONE_DRIVE_CLIENT_SECRET RCLONE_DRIVE_SCOPE
   ```

   rclone takes the client and the scope from those variables, which keeps the secret off the
   command line. Without `RCLONE_DRIVE_CLIENT_ID`, it logs in through its shared client.

   A browser opens on Google's sign-in. Sign in as the remote's account: for `gdrive-mariska`,
   Mariska signs in. If Google says it has not verified the app, choose Advanced, then "Go to
   rclone-backup (unsafe)". If the access screen shows a checkbox for your Google Drive files, tick
   it, then Continue.

   The terminal prints `Paste the following into your remote machine --->`, one line starting
   `{"access_token"`, then `<---End paste`. That line is the account's login: copy it into step
   3's prompt and nowhere else. It keeps, so step 3 can follow hours later.

3. **Write it into `rclone.conf`**, from a terminal in the Ansible KubeCoder pod, between 07:00 and
   22:00 UTC at a minute from :05 to :55, which keeps clear of the nightly jobs and the hourly
   refresh:

   ```bash
   /work/Ansible/scripts/renew-drive-login.sh <remote>
   ```

   It asks for the client ID, then, without echoing, for the client secret and the token line.
   They travel on stdin into `backup-server`'s container, with the pod's
   `~/.kube/config-prd-write`, and never onto a command line. It refuses, writing nothing:

   - outside those times, while a Job in `storage-prd` runs, or while an rclone process runs in
     `backup-server` (its hourly read takes seconds). Run it again a few minutes later. An rclone
     process that read the file before the write and renewed the token after it would write the
     old login back;
   - on a remote that is not a Drive section of the file;
   - on a client ID not ending in `.apps.googleusercontent.com`, a secret not starting `GOCSPX-`,
     or a token that is not one line of JSON with an `access_token` and a `refresh_token`.

   Otherwise it copies the file to `rclone.conf.pre-<remote>-<UTC stamp>` beside it. Then it sets
   the remote's `client_id`, `client_secret` and `token` in one `rclone config update`, which
   leaves the remote's `scope` and every other section as they are. It prints only checks:

   ```
   written at 2026-11-08T09:54:31Z
   copy /data/rclone.conf.pre-gdrive-pieter-20261108T095431Z
   sections: 4 before, 4 after
   [gdrive-pieter] type=drive scope=drive client_id=matches client_secret=set
   refresh_token fingerprint: matches the paste (<12 hex digits>)
   -rw------- root root /data/rclone.conf
   -rw------- root root /data/rclone.conf.pre-gdrive-pieter-20261108T095431Z
   ```

   A count that changed, `WRONG`, `MISSING` or `DIFFERS` means the write went wrong. Put the copy
   back at once, before the next hourly refresh, while no other remote's token has moved since the
   copy was taken:

   ```bash
   kubectl --context prd -n storage-prd exec deploy/backup-server -- cp -p /data/rclone.conf.pre-<remote>-<stamp> /data/rclone.conf
   ```

4. **Check it**, at once, in `backup-server`:

   ```bash
   kubectl --context prd -n storage-prd exec deploy/backup-server -- rclone about <remote>:
   ```

   It prints the account's quota, with no `NOTICE: … shared Google Drive client_id` line and no
   error. Then read the next two hourly refresh runs as in step 1: both show the quota lines, with
   no notice and no error. By the second, rclone has renewed the token with the secret just
   written. For `gdrive-pieter`, `BackupWatcherBlind` also clears at the first scrape that reports
   a successful full read (§2 step 3), and the next night's uploads land. A check that fails takes
   a fresh login: steps 2 and 3 again, the second run taking a copy of its own.

5. **Delete the copies** once step 4's checks pass: each holds the remote's login from before a
   write. Put back later, a copy would also set the other remotes' tokens back, and a OneDrive
   token may have moved on since.

   ```bash
   kubectl --context prd -n storage-prd exec deploy/backup-server -- ls -l /data
   kubectl --context prd -n storage-prd exec deploy/backup-server -- rm /data/rclone.conf.pre-<remote>-<stamp>
   ```
