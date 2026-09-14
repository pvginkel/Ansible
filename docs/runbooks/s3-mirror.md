# S3 mirror restore runbook

Restoring production RGW buckets from their encrypted mirror on Google Drive, reading the mirror
with no cluster at all, and the restore drill. Read this when a production bucket lost or corrupted
objects, when an earlier version of an object is needed, when the whole site is gone, or when
`S3MirrorStale` fires (What can go wrong).

Design context: §"Backup" in [`../../../AnsibleSpecs/decisions.md`](../../../AnsibleSpecs/decisions.md).
The mirror is HelmCharts `charts/storage/files/s3-mirror/s3_mirror.py`, run nightly by the
`s3-mirror` CronJob in `storage-prd` and enabled only in `configs/prd/storage/prd/values.yaml`.

## Conventions

- Every command here is the operator's keystroke. Claude prepares and explains; it does not run
  them.
- Reading a credential — a Kubernetes Secret or an OpenBao value alike — is the operator's
  keystroke or needs the operator's permission for that one value (`CLAUDE.md`, "What Claude
  doesn't read on its own"). The commands below put each value straight into an environment
  variable, so none is printed.
- "Roboform" is the password manager of record.
- Everything runs on one **rclone host**: rclone, kubectl, jq, curl and ssh installed, a browser
  for the Drive login, and LAN reach to `http://ceph:7480` (and to `http://srvk8sdev` for the
  drill). The KubeCoder pod does not qualify: it has no rclone and does not reach srvk8sdev.

## Mirror layout

The mirror is an rclone crypt remote over `gdrive-pieter:Homelab Backups/s3-mirror`. Decrypted:

```
current/<bucket>/<object key>                      live state as of the last successful run
archive/<bucket>/<YYYYMMDDTHHMMSSZ>/<object key>   what the run started at that UTC time replaced or deleted
```

- The job runs at 03:30 cluster-local time and mirrors every bucket owned by an RGW user whose name
  ends in `-prd`.
- An object under `archive/<bucket>/<stamp>/` is the version that was live before the run at
  `<stamp>` replaced or deleted it. A run that moved nothing leaves no folder.
- Each bucket keeps its 30 newest archive folders; an older folder is purged into Drive's trash.
- A bucket gone from RGW keeps its `current/` and `archive/` folders untouched.
- On Drive, file names, folder names and contents are encrypted; without the crypt password and
  salt the folder is unreadable.

## Credentials

| Credential | Where it lives | Used in |
|---|---|---|
| App key pair (read-write) | Secret `s3-credentials` in the app's namespace, written by the release's Terraform (HelmCharts `terraform-modules/s3-storage/main.tf`). No OpenBao or Roboform copy. | §2, §3 — writing into a production bucket |
| `backup-reader` key (read-only) | Secret `backup-reader-credentials` in `storage-prd`, written by the storage release's Terraform (HelmCharts `configs/prd/storage/_shared/infrastructure.tf`) | §5 — reading the live bucket |
| Crypt password and salt | OpenBao, mount `kv`, path `eso/prd/storage/prd/s3-mirror` (fields `password`, `salt`); Roboform, next to the age key | every section |
| Drive login | The Google account that owns `Homelab Backups` | every section |
| Scratch user key | Created on dev Ceph by §5 step 4, removed by its step 8 | §5 |

A release's RGW user is named after its namespace, and today every bucket name starts with it:
`iot-prd-attachments` belongs to the RGW user and namespace `iot-prd`.

## 1 — Set up the remotes

1. **Drive login.** This writes the OAuth token into the host's rclone config, until the
   `rclone config delete` at the end of this section.

   ```bash
   rclone config create gdrive-pieter drive scope=drive
   ```

   Log in in the browser as the account that owns `Homelab Backups`. `scope=drive` sees every file
   in the account, whichever OAuth client uploaded it. On a host without a browser, `config create`
   cannot finish the login: with `config_is_local=false` it prints no `rclone authorize` command
   and saves the remote without a token. Follow rclone's remote setup instead
   (<https://rclone.org/remote_setup/>).

2. **The mirror.** The settings must match the mirror job's
   (HelmCharts `charts/storage/templates/s3-mirror-cronjob.yaml`); any difference decrypts nothing.
   The password and salt are the values from Roboform (OpenBao holds the same pair), put through
   `rclone obscure` as the job does:

   ```bash
   export RCLONE_CONFIG_MIRROR_TYPE=crypt
   export RCLONE_CONFIG_MIRROR_REMOTE='gdrive-pieter:Homelab Backups/s3-mirror'
   export RCLONE_CONFIG_MIRROR_FILENAME_ENCRYPTION=standard
   export RCLONE_CONFIG_MIRROR_FILENAME_ENCODING=base32
   export RCLONE_CONFIG_MIRROR_DIRECTORY_NAME_ENCRYPTION=true
   read -rsp 'crypt password: ' v; echo
   export RCLONE_CONFIG_MIRROR_PASSWORD=$(printf '%s\n' "$v" | rclone obscure -)
   read -rsp 'crypt salt: ' v; echo
   export RCLONE_CONFIG_MIRROR_PASSWORD2=$(printf '%s\n' "$v" | rclone obscure -)
   unset v
   ```

   Check it decrypts:

   ```bash
   rclone lsf mirror:            # archive/ and current/
   rclone lsf mirror:current/    # one line per mirrored bucket
   ```

   Empty output, or `Skipping undecryptable` notices, means a wrong password, salt or setting.

3. **The S3 remotes** — they need a running prd RGW. A helper reads one Secret field; its kubectl
   context must be allowed to read Secrets in the namespace:

   ```bash
   secret() { kubectl --context prd -n "$1" get secret "$2" -o "jsonpath={.data.$3}" | base64 -d; }
   ```

   `app` — the bucket's own read-write key, for writing a restore (§2, §3):

   ```bash
   ns=<the bucket's namespace>
   export RCLONE_CONFIG_APP_TYPE=s3 RCLONE_CONFIG_APP_PROVIDER=Ceph RCLONE_CONFIG_APP_ENDPOINT=http://ceph:7480
   export RCLONE_CONFIG_APP_ACCESS_KEY_ID=$(secret "$ns" s3-credentials access_key_id)
   export RCLONE_CONFIG_APP_SECRET_ACCESS_KEY=$(secret "$ns" s3-credentials secret_access_key)
   ```

   `reader` — `backup-reader`'s read-only key, for comparing against a live bucket (§5):

   ```bash
   export RCLONE_CONFIG_READER_TYPE=s3 RCLONE_CONFIG_READER_PROVIDER=Ceph RCLONE_CONFIG_READER_ENDPOINT=http://ceph:7480
   export RCLONE_CONFIG_READER_ACCESS_KEY_ID=$(secret storage-prd backup-reader-credentials access_key_id)
   export RCLONE_CONFIG_READER_SECRET_ACCESS_KEY=$(secret storage-prd backup-reader-credentials secret_access_key)
   ```

When done, `rclone config delete gdrive-pieter` and close the shell — it holds the keys.

## 2 — Restore a whole bucket

For a production bucket that lost objects or had them overwritten. `rclone copy` puts back what the
mirror holds and deletes nothing: keys added since the last run stay, but a key rewritten since then
goes back to its mirrored version. The bucket must exist:
it and the app's key pair come from the release's Terraform (`terraform-modules/s3-storage`), not
from this runbook.

1. **Suspend the mirror**, so no run moves a half-restored bucket into the archive, and let a
   running `s3-mirror` Job finish:

   ```bash
   kubectl --context prd -n storage-prd patch cronjob s3-mirror -p '{"spec":{"suspend":true}}'
   kubectl --context prd -n storage-prd get jobs
   ```

2. **Set up** §1 steps 1–3, with `app` for the bucket's namespace.

3. **Find the runs since the loss.** Folder names are run start times in UTC; every folder stamped
   after the loss holds versions that run displaced. A run mirrors buckets one at a time for up to
   2 h, so also take the newest folder stamped before the loss if its run had not reached this
   bucket when the loss happened. The last three runs' Jobs are kept; in a run's log, the
   timestamped rclone lines after `==> mirroring <bucket>` show when it got there:

   ```bash
   rclone lsf mirror:archive/<bucket>/
   kubectl --context prd -n storage-prd logs job/<job> | grep -A3 '==> mirroring <bucket>'
   ```

4. **Copy the live state back**, dry run first:

   ```bash
   rclone copy mirror:current/<bucket> app:<bucket> --dry-run
   rclone copy mirror:current/<bucket> app:<bucket> --progress
   ```

5. **Copy each archive folder step 3 found over it, newest first.** The oldest folder is
   copied last, so each object ends at its version from before the loss:

   ```bash
   rclone copy mirror:archive/<bucket>/<stamp> app:<bucket> --progress
   ```

6. **Verify.** This compares contents byte for byte; its differences are at most the keys step 5
   put back from the archive:

   ```bash
   rclone check mirror:current/<bucket> app:<bucket> --one-way --download
   ```

7. **Resume the mirror**, then delete the Drive login and close the shell (§1):

   ```bash
   kubectl --context prd -n storage-prd patch cronjob s3-mirror -p '{"spec":{"suspend":false}}'
   ```

## 3 — Restore single objects or an earlier version

With §1 set up (`app` only when writing into the bucket):

```bash
rclone lsl mirror:current/<bucket>/<key>                   # the version as of the last run
rclone lsl mirror:archive/<bucket> --include '/*/<key>'    # every archived version
```

Each archive line reads `<size> <modified> <stamp>/<key>`: the version that was live before the run
at `<stamp>`. A key containing `*`, `?`, `[` or `{` needs those characters backslash-escaped in the
`--include` pattern. Put a version back into the bucket, or onto local disk:

```bash
rclone copyto 'mirror:archive/<bucket>/<stamp>/<key>' 'app:<bucket>/<key>'
rclone copyto 'mirror:archive/<bucket>/<stamp>/<key>' ./<file>
```

## 4 — Read the mirror with no cluster

The whole-site case: Ceph, both clusters and OpenBao are gone. The Drive login and the crypt
password and salt from Roboform are all it takes.

1. §1 steps 1 and 2 — step 3 needs a running RGW.
2. Copy what is needed onto local disk; earlier versions come from `mirror:archive/` as in §3:

   ```bash
   rclone copy mirror:current/<bucket> ./s3-restore/<bucket> --progress
   ```

3. Once RGW and the app's release are rebuilt, restore the bucket from the mirror through §2.

## 5 — Restore drill

Proves that a fresh Drive login plus the Roboform copy of the crypt key reads the mirror, and that a
bucket restored from it matches the live bucket byte for byte. It restores `iot-prd-attachments`
into a scratch bucket on dev Ceph and never holds a production write key. Run it the morning after
a successful nightly run, so the live bucket has changed as little as possible since.

1. **Confirm a successful run.** Empty output means the mirror has never succeeded — stop.

   ```bash
   kubectl --context prd -n storage-prd get cronjob s3-mirror -o jsonpath='{.status.lastSuccessfulTime}{"\n"}'
   ```

2. **Start srvk8sdev** (VM 919 on `pve`, off by default) and wait for its RGW on port 80:

   ```bash
   ssh root@pve qm start 919
   until curl -s -o /dev/null http://srvk8sdev/; do sleep 10; done
   ```

3. **Set up** §1 steps 1 and 2 with the password and salt typed from Roboform, and the `reader`
   remote from step 3. Not `app`.

4. **Create the scratch user and bucket.** A `restore-drill` user left by an interrupted drill is
   removed with step 8's first command first.

   ```bash
   drill=$(ssh srvk8sdev 'sudo microceph.radosgw-admin user create --uid=restore-drill --display-name=restore-drill')
   export RCLONE_CONFIG_DRILL_TYPE=s3 RCLONE_CONFIG_DRILL_PROVIDER=Ceph RCLONE_CONFIG_DRILL_ENDPOINT=http://srvk8sdev
   export RCLONE_CONFIG_DRILL_ACCESS_KEY_ID=$(jq -r '.keys[0].access_key' <<<"$drill")
   export RCLONE_CONFIG_DRILL_SECRET_ACCESS_KEY=$(jq -r '.keys[0].secret_key' <<<"$drill")
   unset drill
   rclone mkdir drill:restore-drill
   ```

5. **Restore from Drive:**

   ```bash
   time rclone copy mirror:current/iot-prd-attachments drill:restore-drill --progress
   rclone size drill:restore-drill
   ```

6. **Compare with the live bucket**, read with `backup-reader`'s key. `--download` compares
   contents byte for byte:

   ```bash
   rclone check drill:restore-drill reader:iot-prd-attachments --download
   ```

   Pass: `0 differences found`. A difference is a mirror fault unless the live object was written
   or deleted after the run's start (`rclone lsl reader:iot-prd-attachments/<key>` shows the
   modification time).

7. **Record** the date, the `lastSuccessfulTime`, the `rclone size` output, the copy's duration and
   the check's summary lines in the drill log below.

8. **Clean up** — the scratch user with its bucket and objects, the Drive login, srvk8sdev (leave it
   up if something else needs it), then close the shell:

   ```bash
   ssh srvk8sdev 'sudo microceph.radosgw-admin user rm --uid=restore-drill --purge-data'
   rclone config delete gdrive-pieter
   ssh root@pve qm shutdown 919
   ```

## Drill log

- **First drill** — _pending the first successful mirror run. Date, `lastSuccessfulTime`,
  `rclone size`, copy duration, `rclone check` summary._

## What can go wrong

- **`rclone lsf mirror:` lists nothing, or `Skipping undecryptable` notices** — wrong crypt
  password, salt or setting (§1 step 2).
- **`AccessDenied` from `reader:` on a production bucket** — the bucket's policy lacks the
  `backup-reader` grant. `terraform-modules/s3-storage` sets it when the release deploys into a
  `-prd` namespace; the nightly mirror fails on that bucket too.
- **`http://srvk8sdev/` never answers** — the VM is still booting, or microceph's RGW is down:
  `ssh srvk8sdev 'sudo microceph status'`.
- **`S3MirrorStale` fires** — the `s3-mirror` CronJob has not succeeded for 52 h, so two nightly
  runs failed. The copy already on Drive stays readable. Find the newest Job with
  `kubectl --context prd -n storage-prd get jobs`, then read its log with
  `kubectl --context prd -n storage-prd logs job/<name>`:
  - `listing buckets failed` or `no bucket is owned by a prd-stage user` — the run stopped at the
    admin-API bucket listing as `backup-reader`, before any sync.
  - `FAILED buckets: …` — those buckets' sync or prune failed; the other buckets were mirrored.
  - A log that stops without `all buckets mirrored` — the Job was killed, for instance at its 2 h
    `activeDeadlineSeconds`.

## Pre-flight checklist

- [ ] The crypt password and salt are in Roboform — the drill proves that copy.
- [ ] You can log in to the Google account that owns `Homelab Backups`.
- [ ] An rclone host as described under Conventions is at hand.
