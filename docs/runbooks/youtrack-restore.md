# YouTrack restore runbook

Restoring the self-hosted YouTrack (`issues.webathome.org`) from its nightly backup on Google Drive,
and the restore drill. Read this when YouTrack's database is lost or corrupted, when the whole site
is gone, or when `YouTrackBackupStale` fires (What can go wrong).

Design context: §"Backup" in [`../../../AnsibleSpecs/decisions.md`](../../../AnsibleSpecs/decisions.md).
The backup is HelmCharts `charts/youtrack/files/backup/backup.py`, run nightly by the
`youtrack-backup` CronJob in `youtrack-prd` and enabled only in `configs/prd/youtrack/prd/values.yaml`.

## Conventions

- Every command here is the operator's keystroke. Claude prepares and explains; it does not run
  them.
- Reading a credential — a Kubernetes Secret or an OpenBao value alike — is the operator's
  keystroke or needs the operator's permission for that one value (`CLAUDE.md`, "What Claude
  doesn't read on its own").
- "Roboform" is the password manager of record.
- Everything runs on one **restore host**: rclone, age, Docker, kubectl and a browser, the
  browser for the Drive login and YouTrack's configuration wizard. The KubeCoder pod does not
  qualify: it has no rclone or Docker.

## What a backup is

YouTrack writes the backup itself, while it runs: one `.tar.gz` of its embedded database, which
holds the issues, their attachments and the built-in Hub (users, groups, tokens, the Keycloak auth
module, the backup settings). `logs/` is not in it, and of `conf/` only YouTrack's internal part;
the chart rewrites the rest on every start. The first one, on 2026-09-17, took 5 s: an 18.7 MB
archive of 87 entries — `youtrack/` (the database files, and `blobs/` holding the attachments),
`hub/` and `conf/internal/`.

- The CronJob runs at 01:30 cluster-local time. It asks YouTrack for a backup over the REST API,
  as the `backup` service user, and POSTs the archive to `backup-server`.
- `backup-server` age-encrypts it to the operator's key and stores it as
  `gdrive-pieter:Homelab Backups/youtrack/<YYYYMMDDTHHMMSSZ>_youtrack.tar.gz.age`. The stamp is the
  upload's time in UTC. The 30 newest objects are kept.
- YouTrack also keeps its 3 newest archives in `backups/` on its own volume, named
  `<YYYY-MM-DD-HH-MM-SS>.tar.gz`. They die with the volume.
- A backup restores only into the same or a newer YouTrack version. Use the image tag production
  runs, from HelmCharts `charts/youtrack/values.yaml`.

## Credentials

| Credential | Where it lives | Used in |
|---|---|---|
| age private key | Roboform only; `backup-server` holds the public half (OpenBao runbook §3) | every section |
| Drive login | The Google account that owns `Homelab Backups` | every section |
| A local YouTrack administrator | Inside the backup, so the password that held on the backup's date. Keycloak sign-in does not work on a host name other than `issues.webathome.org`. | §3 |
| `backup` service user token | OpenBao, mount `kv`, path `eso/prd/youtrack/prd/backup` (field `token`); a Hub user, so a restore brings the user and token back | What can go wrong |

## 1 — Fetch and decrypt a backup

1. **Drive login**, as in [`s3-mirror.md`](s3-mirror.md) §1 step 1:

   ```bash
   rclone config create gdrive-pieter drive scope=drive
   ```

2. **Pick the object.** The newest is last:

   ```bash
   rclone lsl 'gdrive-pieter:Homelab Backups/youtrack/'
   ```

3. **Copy and decrypt it.** The plaintext is the whole tracker: keep it on the restore host and
   delete it when done.

   ```bash
   mkdir -p ~/youtrack-restore && cd ~/youtrack-restore
   rclone copy 'gdrive-pieter:Homelab Backups/youtrack/<stamp>_youtrack.tar.gz.age' .
   age -d -i <age-key-from-Roboform> <stamp>_youtrack.tar.gz.age > youtrack-<stamp>.tar.gz
   tar tzf youtrack-<stamp>.tar.gz | head
   ```

   `tar` listing entries means the archive decrypted whole.

## 2 — Restore production

Replaces YouTrack's database with the backup's; everything written since is lost. This is
JetBrains' Docker restore procedure (empty `data/` and `conf/`, put the archive in `backups/`, pick
it as the upgrade source in the configuration wizard) applied to the chart's one RBD volume, where
the four directories are subpaths. No `youtrack` deploy may run while this is in progress: it would
scale the Deployment back up.

1. **Stop YouTrack and the backup.**

   ```bash
   kubectl --context prd -n youtrack-prd patch cronjob youtrack-backup -p '{"spec":{"suspend":true}}'
   kubectl --context prd -n youtrack-prd scale deployment youtrack --replicas=0
   kubectl --context prd -n youtrack-prd wait --for=delete pod -l app=youtrack --timeout=300s
   ```

2. **Mount the volume in a helper pod**, as YouTrack's uid:

   ```bash
   kubectl --context prd -n youtrack-prd apply -f - <<'EOF'
   apiVersion: v1
   kind: Pod
   metadata:
     name: youtrack-restore
   spec:
     securityContext:
       runAsUser: 13001
       runAsGroup: 13001
       fsGroup: 13001
     containers:
       - name: shell
         image: busybox
         command: ["sleep", "86400"]
         volumeMounts:
           - name: youtrack
             mountPath: /mnt
     volumes:
       - name: youtrack
         persistentVolumeClaim:
           claimName: youtrack-prd-data-pvc
   EOF
   kubectl --context prd -n youtrack-prd wait --for=condition=Ready pod/youtrack-restore
   ```

3. **Set the old database aside and put the archive in place.** Moving rather than deleting keeps
   a way back until step 6 passes; the Deployment's init container recreates `data/` and `conf/`.

   ```bash
   kubectl --context prd -n youtrack-prd exec youtrack-restore -- sh -c \
     'mv /mnt/data /mnt/data.before-restore && mv /mnt/conf /mnt/conf.before-restore'
   kubectl --context prd -n youtrack-prd cp youtrack-<stamp>.tar.gz youtrack-restore:/mnt/backups/restore-<stamp>.tar.gz
   kubectl --context prd -n youtrack-prd exec youtrack-restore -- sh -c \
     'chmod 750 /mnt/backups/restore-<stamp>.tar.gz && df -h /mnt && ls -ln /mnt /mnt/backups'
   kubectl --context prd -n youtrack-prd delete pod youtrack-restore
   ```

   The volume has to fit the old database, the archive and the restored database at once;
   `df` shows whether it does. If not, delete `data.before-restore` first and lose the way back.

4. **Start YouTrack and open the wizard.** With `data/` empty it starts the configuration wizard
   instead of the tracker, and logs a URL carrying a `wizard_token`. Reach it through a
   port-forward, not the public host name:

   ```bash
   kubectl --context prd -n youtrack-prd scale deployment youtrack --replicas=1
   kubectl --context prd -n youtrack-prd logs -f deployment/youtrack | grep -m1 wizard_token
   kubectl --context prd -n youtrack-prd port-forward deployment/youtrack 8080:8080
   ```

   Open `http://localhost:8080/?wizard_token=<token>`.

5. **Restore in the wizard:** choose **Upgrade**, select `restore-<stamp>.tar.gz` as the upgrade
   source, confirm the storage locations (`/opt/youtrack/data`, `/opt/youtrack/backups`) and click
   **Upgrade**. When it finishes, YouTrack starts on the restored database.

6. **Verify** at `https://issues.webathome.org`: sign in through Keycloak, open a recent issue with
   an attachment, and check that the newest issues are the ones from before the backup's stamp. Then
   take a fresh backup, which also proves the restored `backup` token:

   ```bash
   kubectl --context prd -n youtrack-prd create job youtrack-backup-after-restore --from=cronjob/youtrack-backup
   kubectl --context prd -n youtrack-prd logs -f job/youtrack-backup-after-restore
   ```

   The log ends in `youtrack backed up`.

7. **Resume the backup and clean up** — the set-aside directories, the archive on the volume, the
   plaintext on the restore host and the Drive login:

   ```bash
   kubectl --context prd -n youtrack-prd patch cronjob youtrack-backup -p '{"spec":{"suspend":false}}'
   kubectl --context prd -n youtrack-prd delete job youtrack-backup-after-restore
   ```

   Remove `/mnt/data.before-restore`, `/mnt/conf.before-restore` and
   `/mnt/backups/restore-<stamp>.tar.gz` through a helper pod as in step 2, then
   `rm -rf ~/youtrack-restore` and `rclone config delete gdrive-pieter`.

## 3 — Restore drill

Proves that a fresh Drive login plus the Roboform age key yield a backup that YouTrack restores, with
the chart's own start arguments. It restores into a throwaway container on the restore host and
never touches production. Run it the morning after a successful nightly run, so production has
changed as little as possible since.

1. **Confirm a successful run.** Empty output means the backup has never succeeded — stop.

   ```bash
   kubectl --context prd -n youtrack-prd get cronjob youtrack-backup -o jsonpath='{.status.lastSuccessfulTime}{"\n"}'
   ```

2. **Fetch and decrypt the newest backup** (§1).

3. **Lay out the four directories** the container mounts, owned by YouTrack's uid, with the archive
   in `backups/`:

   ```bash
   mkdir -p ~/youtrack-restore/drill/{data,conf,logs,backups}
   mv ~/youtrack-restore/youtrack-<stamp>.tar.gz ~/youtrack-restore/drill/backups/restore-<stamp>.tar.gz
   chmod 750 ~/youtrack-restore/drill/backups/restore-<stamp>.tar.gz
   sudo chown -R 13001:13001 ~/youtrack-restore/drill
   ```

4. **Start YouTrack** with production's image tag and the chart's arguments, the base URL
   pointed at the drill:

   ```bash
   cd ~/youtrack-restore/drill
   docker run -d --name youtrack-drill -p 127.0.0.1:8080:8080 \
     -v "$PWD/data:/opt/youtrack/data" -v "$PWD/conf:/opt/youtrack/conf" \
     -v "$PWD/logs:/opt/youtrack/logs" -v "$PWD/backups:/opt/youtrack/backups" \
     jetbrains/youtrack:<production tag> \
     --allow.configure.and.run -J-Xmx1g --base-url=http://localhost:8080
   docker logs -f youtrack-drill 2>&1 | grep -m1 wizard_token
   ```

5. **Restore in the wizard** at `http://localhost:8080/?wizard_token=<token>` as in §2 step 5, and
   time it from **Upgrade** to the sign-in page.

6. **Check.** Pass when all four hold:
   - A local administrator signs in. Keycloak sign-in failing here is expected.
   - Every project's issue count matches production's. A difference is a backup fault unless
     production gained or lost those issues after 01:30; a search for `created: Today` there shows
     the gains.
   - One attachment opens.
   - One issue's comments and history match production's.

7. **Record** the date, the `lastSuccessfulTime`, the Drive object name, the archive's size, the
   wizard's duration and the per-project counts in the drill log below.

8. **Clean up** — the container, the plaintext tracker and the Drive login:

   ```bash
   docker rm -f youtrack-drill
   sudo rm -rf ~/youtrack-restore
   rclone config delete gdrive-pieter
   ```

## Drill log

- **First drill** — _pending the first successful nightly backup. Date, `lastSuccessfulTime`,
  object, archive size, wizard duration, per-project counts._

## What can go wrong

- **`YouTrackBackupStale` fires** — the `youtrack-backup` CronJob has not succeeded for 52 h, so
  two nightly runs failed. The copies already on Drive stay restorable. Find the newest Job with
  `kubectl --context prd -n youtrack-prd get jobs`, then read its log with
  `kubectl --context prd -n youtrack-prd logs job/<name>`:
  - `HTTP 401` or `HTTP 403 from http://youtrack/...` — YouTrack refused the `backup` user's token:
    revoked, or the user lost Low-level Admin Read or Write. A new token goes into OpenBao at
    `eso/prd/youtrack/prd/backup`; ESO picks it up within the hour.
  - `YouTrack reported a backup error` — YouTrack could not write the archive, most often because
    its 10 GiB volume is full.
  - `no new backup within 1200 s` — YouTrack finished, or never started, without writing a new
    archive.
  - `downloaded N bytes, but YouTrack lists … at M` — the download was cut short.
  - `HTTP 403 from http://youtrack/api/admin/backups/…` — the download link was refused. YouTrack
    signs a fresh link on every listing and honours each one once, so a link reused or taken
    from an older listing gets 403.
  - `HTTP 500 from http://backup-server…` — `backup-server` could not encrypt or store it; read
    the `backup-server` pod's log in `storage-prd`.
  - A log that stops without `youtrack backed up` — the Job was killed, for instance at its 1 h
    `activeDeadlineSeconds`.
- **The Job never starts a container: `CreateContainerConfigError`** — a Secret is missing:
  `youtrack-backup` (ESO, from OpenBao `eso/prd/youtrack/prd/backup`) or `youtrack-backup-upload`
  (the release's Terraform).
- **The wizard lists no backup** — the archive is not in `backups/`, or uid 13001 cannot read it.
- **The wizard refuses the backup** — it comes from a newer YouTrack than the image; run
  production's tag.
- **No `wizard_token` line in the log, and YouTrack starts empty** — the chart's start arguments
  skip the wizard. Stop, and record it in the drill log: §2 needs a different restore step.

## Pre-flight checklist

- [ ] The age private key is in Roboform — the drill proves that copy.
- [ ] You can log in to the Google account that owns `Homelab Backups`.
- [ ] You know a local YouTrack administrator's password.
- [ ] A restore host as described under Conventions is at hand.
