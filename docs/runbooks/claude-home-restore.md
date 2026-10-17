# Claude home restore runbook

Restoring the shared Claude Code home, `~/.claude` and `~/.claude.json`, from its nightly backup on
Google Drive; reading a copy with no cluster; and the restore drill. Read this when `~/.claude` is
lost or damaged — its sessions, memory, settings or the claude.ai login — when the whole site is
gone, or when `BackupOverdue` fires for the `claude-home` scope (What can go wrong).

Design context: §"Backup" in [`../../../AnsibleSpecs/decisions.md`](../../../AnsibleSpecs/decisions.md).
The backup is KubeCoderDeploy's `chart/files/claude-home-backup/backup.py`, run nightly by the
`claude-home-backup` CronJob in `kubecoder-prd` and enabled only in `config/prd/values.yaml`
(`claudeHomeBackup.enabled`). KubeCoderDeploy reaches prd only through KubeCoder's promotion.

## Conventions

- Every command here is the operator's keystroke. Claude prepares and explains; it does not run
  them.
- Reading a credential — a Kubernetes Secret or an OpenBao value alike — is the operator's
  keystroke or needs the operator's permission for that one value (`CLAUDE.md`, "What Claude
  doesn't read on its own").
- "Roboform" is the password manager of record.
- Everything runs on one **restore host**: rclone, age, kubectl with write access to
  `kubecoder-prd`, and a browser for the Drive login; §4 also needs zstd and GNU tar, or Python
  3.14. The KubeCoder pod does not qualify: it has no rclone, and §2 stops every environment.

## What a backup is

One tar of the whole of `~/.claude`, the directory every KubeCoder environment mounts as
`/home/ubuntu/.claude`: `system/home/.claude` on KubeCoder's ZFS claim, on srvk8s4. Nothing in it is
left out, the claude.ai login in `.credentials.json` included. On 2026-10-17 the tree held 48,394
entries and 10 GB, which compress to about 1.7 GB.

- The CronJob runs at 00:30 node-local time. It streams the tar, compressed as it goes (zstd level
  3, long-distance matching, a 2^27 window), to `backup-server`'s in-cluster Service. Sessions
  write all night: a file that grows while it is read is archived at the size it had when the read
  reached it, and an entry that vanishes first is skipped and logged as `vanished:`.
- `backup-server` age-encrypts it to the operator's key and stores it as
  `gdrive-pieter:Homelab Backups/claude-home/<YYYYMMDDTHHMMSSZ>_claude-home.tar.zst.age`, with a
  `.metadata.json` beside it. The stamp is the upload's start in UTC. The 3 newest objects are
  kept; a pruned one bypasses the Drive trash.
- Members are named `.claude/…`. Each symlink is archived as the link itself, never followed. Of
  the 327 links on 2026-10-17, 323 point at absolute targets under `/home/ubuntu/.claude/` (the
  subagent transcripts). They resolve only in a copy that sits at `/home/ubuntu/.claude`. A copy
  anywhere else on the shared home has links that point into the live `~/.claude`; on another
  machine they point at nothing.
- `~/.claude.json`, Claude Code's main config, sits beside `~/.claude` and is not in the archive.
  The copies Claude Code keeps rotating in `~/.claude/backups/.claude.json.backup.<ms>` are, and
  the restore puts the newest one back.
- Decompressing needs zstd, or Python 3.14 (`compression.zstd`). Python 3.14's default extraction
  filter refuses the absolute links, so every extraction here passes `filter="fully_trusted"`, which
  also keeps the files' modes. §2 and §3 decompress in a helper pod on the backup job's own image,
  so the restore host needs neither.

## Credentials

| Credential | Where it lives | Used in |
|---|---|---|
| age private key | Roboform only; `backup-server` holds the public half (OpenBao runbook §3) | every section |
| Drive login | The Google account that owns `Homelab Backups` | every section |
| claude.ai login | Inside the copy, `.claude/.credentials.json` | §2, What can go wrong |
| `claude-home` upload token | Secret `claude-home-backup-upload` in `kubecoder-prd`, key `token`, written by KubeCoderDeploy's `terraform/backup.tf` | What can go wrong |

## 1 — Fetch and decrypt a backup

1. **Drive login**, as in [`s3-mirror.md`](s3-mirror.md) §1 step 1:

   ```bash
   rclone config create gdrive-pieter drive scope=drive
   ```

2. **Pick the object**, a `.tar.zst.age`. The backup Job's log names the one it uploaded (§3 step
   1).

   ```bash
   rclone lsl 'gdrive-pieter:Homelab Backups/claude-home/'
   ```

3. **Copy and decrypt it.** The plaintext holds the claude.ai login: keep it on the restore host
   and delete it when done.

   ```bash
   mkdir -p ~/claude-home-restore && cd ~/claude-home-restore
   rclone copy 'gdrive-pieter:Homelab Backups/claude-home/<stamp>_claude-home.tar.zst.age' .
   age -d -i <age-key-from-Roboform> <stamp>_claude-home.tar.zst.age > claude-home-<stamp>.tar.zst
   ```

   age exits 0 only for an object that decrypts whole: it authenticates every chunk, and the end.

## 2 — Restore the home

Replaces `~/.claude` and `~/.claude.json` with the copy's, for every environment at once: everything
written since the backup's stamp leaves the home, the transcripts of sessions since included, and
only the set-aside tree keeps it until step 8. The copy is unpacked beside the live home first,
which is safe while environments run. Only the swap in step 6 needs every environment stopped. To
bring back only some files — a lost transcript, a project's memory — stop after step 4 and copy
them out of `~/.claude-restore-<stamp>/.claude/` from any environment with `cp -a`, which copies
links as links. Then clean up as in §3 step 6, and resume the backup with step 8's first command.

After a rebuild of KubeCoder, bring one environment up and stop it again before step 3. Its start
creates the home, owned by uid 1000, that the helper pod mounts.

1. **Stop the backup.** Each night it runs on a damaged home, its upload pushes the oldest good copy
   off Drive. Left suspended, it fires `BackupOverdue` after 52 h.

   ```bash
   kubectl --context prd -n kubecoder-prd patch cronjob claude-home-backup -p '{"spec":{"suspend":true}}'
   ```

2. **Fetch and decrypt the copy** (§1).

3. **Mount the home in a helper pod**, on the backup job's image (Python 3.14), as uid 1000, the
   files' owner. It sets no `fsGroup`, which would have the kubelet re-own the whole claim, every
   environment's storage with it. It tolerates srvk8s4's taint; the claim pins it to srvk8s4.

   ```bash
   IMAGE=$(kubectl --context prd -n kubecoder-prd get cronjob claude-home-backup \
     -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].image}')
   kubectl --context prd -n kubecoder-prd apply -f - <<EOF
   apiVersion: v1
   kind: Pod
   metadata:
     name: claude-home-restore
   spec:
     securityContext:
       runAsUser: 1000
       runAsGroup: 1000
     tolerations:
       - key: homelab.local/performance
         operator: Equal
         value: high
         effect: NoSchedule
     containers:
       - name: shell
         image: $IMAGE
         command: ["sleep", "86400"]
         volumeMounts:
           - name: home
             mountPath: /home/ubuntu
             subPath: system/home
     volumes:
       - name: home
         persistentVolumeClaim:
           claimName: kubecoder-prd-zfs-pvc
   EOF
   kubectl --context prd -n kubecoder-prd wait --for=condition=Ready pod/claude-home-restore --timeout=300s
   ```

4. **Unpack the copy beside the live home**, as `~/.claude-restore-<stamp>/.claude`, which every
   environment sees. The free space `df` shows has to exceed what `du` shows for the live tree.

   ```bash
   kubectl --context prd -n kubecoder-prd exec claude-home-restore -- sh -c 'df -h /home/ubuntu && du -sh /home/ubuntu/.claude'
   time kubectl --context prd -n kubecoder-prd exec -i claude-home-restore -- python3 -c 'import sys, tarfile; tarfile.open(fileobj=sys.stdin.buffer, mode="r|zst").extractall(sys.argv[1], filter="fully_trusted")' /home/ubuntu/.claude-restore-<stamp> < claude-home-<stamp>.tar.zst
   kubectl --context prd -n kubecoder-prd exec claude-home-restore -- sh -c 'cd /home/ubuntu/.claude-restore-<stamp> && find .claude | wc -l'
   ```

   The count is the archive's whole: the backup Job's log gives the same number (§3 step 1).

5. **Stop every environment**, from the bot, the MCP adapter or VS Code, then check that none
   runs:

   ```bash
   kubectl --context prd -n kubecoder-prd get pods -l app.kubernetes.io/managed-by=kubecoder
   ```

   `No resources found` means none. A timer run can start one at any time, so run step 6 right
   after this check.

6. **Swap the copy in.** Whatever `.claude` and `.claude.json` the home holds are set aside, not
   deleted, as a way back until step 7 passes. `~/.claude.json` comes from the newest
   `.claude.json.backup.*` in the copy.

   ```bash
   kubectl --context prd -n kubecoder-prd exec claude-home-restore -- sh -c 'set -e; cd /home/ubuntu
     [ ! -e .claude ] || mv .claude .claude.before-restore-<stamp>
     [ ! -e .claude.json ] || mv .claude.json .claude.json.before-restore-<stamp>
     mv .claude-restore-<stamp>/.claude .claude && rmdir .claude-restore-<stamp>
     cp -p "$(ls -t .claude/backups/.claude.json.backup.* | head -n 1)" .claude.json
     ls -ld .claude .claude.json && python3 -m json.tool .claude.json > /dev/null && echo restored'
   ```

   The output ends in `restored`. In place, the absolute links resolve again.

7. **Verify.** Start one environment and open a Claude session in it. It is signed in, and
   `/resume` lists the sessions up to the backup's stamp. Then start the other environments.

8. **Resume the backup and clean up**: the set-aside home, the helper pod, the plaintext on the
   restore host and the Drive login. `rm -rf` removes the set-aside tree's links, never what they
   point at.

   ```bash
   kubectl --context prd -n kubecoder-prd patch cronjob claude-home-backup -p '{"spec":{"suspend":false}}'
   kubectl --context prd -n kubecoder-prd exec claude-home-restore -- \
     rm -rf /home/ubuntu/.claude.before-restore-<stamp> /home/ubuntu/.claude.json.before-restore-<stamp>
   kubectl --context prd -n kubecoder-prd delete pod claude-home-restore
   rm -rf ~/claude-home-restore
   rclone config delete gdrive-pieter
   ```

## 3 — Restore drill

Proves that a fresh Drive login plus the Roboform age key yield a copy that unpacks whole into a
usable home. The copy is unpacked beside the live home, and the live home is never touched. Run it
the morning after a successful nightly run.

1. **Confirm a successful run, and read what it archived.** Empty output from the first command
   means the backup has never succeeded — stop. In the newest `claude-home-backup-…` Job's log, the
   `==> N entries archived` line gives the count, and the `-> 201` line names the Drive object.

   ```bash
   kubectl --context prd -n kubecoder-prd get cronjob claude-home-backup -o jsonpath='{.status.lastSuccessfulTime}{"\n"}'
   kubectl --context prd -n kubecoder-prd get jobs
   kubectl --context prd -n kubecoder-prd logs job/<name>
   ```

2. **Fetch and decrypt that object** (§1).

3. **Unpack it beside the live home**: §2 steps 3 and 4.

4. **Check the copy, reading its links as links.** In the helper pod and in every environment, the
   copy's absolute links point into the live `~/.claude`. Anything that reads through them — `cat`,
   `diff -r`, `grep -R`, `find -L`, `du -L` — reads the live home and shows a copy that is not
   there, and a write through them changes the live home. `find -type l` and `ls -l` never follow
   a link.

   ```bash
   kubectl --context prd -n kubecoder-prd exec claude-home-restore -- sh -c 'set -e; cd /home/ubuntu/.claude-restore-<stamp>
     find .claude | wc -l
     find .claude -type l | wc -l
     find .claude -type l -exec ls -l {} + | head -n 5
     stat -c "%a %u:%g %y %n" .claude .claude/.credentials.json .claude/history.jsonl
     python3 -m json.tool .claude/.credentials.json > /dev/null
     python3 -m json.tool "$(ls -t .claude/backups/.claude.json.backup.* | head -n 1)" > /dev/null
     echo both parse'
   ```

   Pass when all of these hold:
   - The first count equals the log's `N entries archived`.
   - The copy holds links (327 in the live tree on 2026-10-17), and `ls -l` shows each as
     `name -> target`, the subagent ones `-> /home/ubuntu/.claude/projects/…`.
   - `.claude` is `700` and `.credentials.json` `600`, both `1000:1000`, and `history.jsonl`'s time
     (UTC) is that of the last prompt given before the backup ran.
   - The output ends in `both parse`: the claude.ai login and the newest `.claude.json` copy are
     whole JSON.

5. **Record** the date, the `lastSuccessfulTime`, the Drive object name, the `.tar.zst`'s size,
   the entry count against the log's, the link count and the unpack time in the drill log below.

6. **Clean up** — the copy, the helper pod, the plaintext on the restore host and the Drive login.
   `rm -rf` removes the copy's links, never what they point at.

   ```bash
   kubectl --context prd -n kubecoder-prd exec claude-home-restore -- rm -rf /home/ubuntu/.claude-restore-<stamp>
   kubectl --context prd -n kubecoder-prd delete pod claude-home-restore
   rm -rf ~/claude-home-restore
   rclone config delete gdrive-pieter
   ```

## 4 — Read a copy without the cluster

For when KubeCoder is down and something in `~/.claude` is needed. Unpack a copy fetched and
decrypted as in §1 on the restore host, with zstd and GNU tar (`-p` keeps the files' modes past the
umask):

```bash
cd ~/claude-home-restore && mkdir copy
tar --zstd -xpf claude-home-<stamp>.tar.zst -C copy
```

or with Python 3.14:

```bash
cd ~/claude-home-restore && mkdir copy
python3.14 -c 'import sys, tarfile; tarfile.open(sys.argv[1], mode="r:zst").extractall(sys.argv[2], filter="fully_trusted")' claude-home-<stamp>.tar.zst copy
```

The absolute links point at `/home/ubuntu/.claude/…` on the restore host. Read them as links, as in
§3 step 4. `rm -rf ~/claude-home-restore` removes the copy and the plaintext when done.

## Drill log

- **First drill** — _pending the first nightly backup after KubeCoder's promotion. Date,
  `lastSuccessfulTime`, object, `.tar.zst` size, entries against the log's, links, unpack time._

## What can go wrong

- **`BackupOverdue` fires for `scope="claude-home"`** — no upload has landed on `backup-server` for
  52 h, so two nightly runs failed or the CronJob is suspended (§2 step 1). The copies already on
  Drive stay restorable. If `BackupWatcherBlind` fires too, start at
  [`backup-freshness.md`](backup-freshness.md) §2. Find the newest Job with
  `kubectl --context prd -n kubecoder-prd get jobs`, then read its log with
  `kubectl --context prd -n kubecoder-prd logs job/<name>`:
  - `HTTP 401 from http://backup-server…` — `backup-server` refused the upload token: the
    `claude-home` credential is revoked, or the Secret holds another one.
  - `HTTP 413 from http://backup-server…` — the compressed archive outgrew `backup-server`'s 10 GiB
    upload cap.
  - `HTTP 500 from http://backup-server…` — `backup-server` could not encrypt or store it; read the
    `backup-server` pod's log in `storage-prd`.
  - `ERROR: sending to http://backup-server…`, or an `ERROR: [Errno …]` naming no file, such as
    `Connection refused` — the connection to `backup-server` failed or broke: it is down or
    unreachable.
  - `ERROR: reading .claude/…` — a file could not be read whole, for instance one that shrank while
    it was read. The next night's run starts over.
  - `ERROR: … is a socket, which tar cannot hold` — something left a socket under `~/.claude`.
  - A log that stops without `claude-home backed up` — the Job was killed, at its 1 h
    `activeDeadlineSeconds` or for memory.
- **The Job never starts a container: `CreateContainerConfigError`** — Secret
  `claude-home-backup-upload` is missing; KubeCoderDeploy's Terraform writes it, on prd only.
- **`age: error: no identity matched any of the recipients`** — the key is not the one
  `backup-server` encrypts to.
- **`age: error: failed to decrypt and authenticate payload chunk`** — the object is cut short or
  damaged; fetch it again, or take an older one.
- **The unpack stops with `tarfile.ReadError: unexpected end of data`** — the stream broke, or the
  `.tar.zst` is cut short. It leaves a partial `~/.claude-restore-<stamp>`: remove it and unpack
  again.
- **The unpack stops with `tarfile.AbsoluteLinkError`** — the command lost its
  `filter="fully_trusted"`. Remove the partial tree and unpack again with it.
- **The unpack stops with `PermissionError`** — the home is not uid 1000's: on a rebuilt KubeCoder,
  no environment has come up yet (§2).
- **Claude Code asks for a login after the restore** — the copy's claude.ai login no longer holds.
  Log in once, with `/login`, from any environment; the shared home makes it every environment's.

## Pre-flight checklist

- [ ] The age private key is in Roboform — the drill proves that copy.
- [ ] You can log in to the Google account that owns `Homelab Backups`.
- [ ] A restore host as described under Conventions is at hand.
