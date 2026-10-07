# OpenBao operator runbook

Day-to-day administration and disaster recovery for the homelab
OpenBao cluster. Read this when you need to reach the cluster as an
admin, recover a lost node, recover the whole cluster, or read a
secret out of a backup.

Design context:
[`../../../AnsibleSpecs/phases/completed/openbao.md`](../../../AnsibleSpecs/phases/completed/openbao.md),
"Secrets — OpenBao" and "OpenBao backup / DR" in
[`../../../AnsibleSpecs/decisions.md`](../../../AnsibleSpecs/decisions.md),
and the role README at
[`../../ansible/roles/openbao/README.md`](../../ansible/roles/openbao/README.md).
The IaC-agent escape hatch is [`iac-cold-boot.md`](iac-cold-boot.md).

## Conventions

- The operator runs every `terraform` and `ansible-playbook`
  invocation. Claude prepares changes; it does not apply them.
- "Roboform" is the password manager of record: it holds the
  ansible-vault passphrase, the Shamir recovery keys (3-of-5), and
  the age private key for backup decryption.
- Ansible runs from `ansible/`; Terraform from `terraform/prd/`.
- Once `openbao_ufw_enable` is `true`, `srvvaultN` SSH is reachable
  from `srviac` only — drive recovery from `srviac` (or flip ufw off
  out-of-band first; see the role README §Locking down with ufw).

## Cluster facts

| Host | VMID | PVE node | IPv4 | Raft role |
|---|---|---|---|---|
| `srvvault1` | 913 | `pve` | `10.1.0.40` | bootstrap candidate |
| `srvvault2` | 914 | `pve1` | `10.1.0.41` | — |
| `srvvault3` | 915 | `pve2` | `10.1.0.42` | — |

- **Client endpoint**: `https://secrets/` — leader-tracking VIP
  `secrets.home` (`10.1.0.39`), HAProxy 443 → 8200.
- **Direct node API**: `https://srvvaultN.home:8200`.
- **Seal**: static auto-unseal. The key is ansible-vault'd at
  `roles/openbao/files/static.key`; its id is
  `openbao_seal_current_key_id` in
  `inventories/prd/group_vars/openbao.yml`.
- **Root token**: retired. Mint a fresh one from the
  Shamir recovery keys with `bao operator generate-root` when an
  admin path outside the `openbao-admin` AppRole is needed.
- **Convergence playbook**: `playbooks/site-openbao.yml` — runs
  bootstrap → baseline → managed_filesystems → openbao →
  ssh_host_cert against the `openbao` group.
- **Listener cert renewal**: `playbooks/renew-internal-tls.yml`, run
  weekly by `IaC/Scheduled Certs`, reissues the listener leaf on each
  peer once it is inside its 14-day window. It reaches the hosts over
  the plain `ansible.cfg` args — no Terraform `known_hosts` handoff, no
  Proxmox credentials — so it can run without the setup
  `site-openbao.yml` needs. The window allows two attempts before a leaf
  lapses, so a red Friday is worth chasing before the next one. The
  daily `IaC/Scheduled Drift` build reds once a peer's leaf is under
  7 days — a Friday that was missed, not one still to come. A leaf that
  has already lapsed: [`internal-tls-expiry.md`](internal-tls-expiry.md).

## 1 — Admin access

Routine reads/writes go through a consumer's AppRole, not an admin
session. For interactive administration:

- **Web UI** — `https://secrets/ui/` in a browser. The listener cert
  chains to the homelab CA, so it validates on any `.home` client
  with the CA trust root. Log in with the `openbao-admin` AppRole or
  a recovery-key-minted root token.
- **CLI** — on a host with `/usr/bin/bao` (every `srvvaultN`, or
  `srviac`):

  ```bash
  export BAO_ADDR=https://secrets
  export BAO_TOKEN=$(bao write -field=token auth/approle/login \
      role_id=<openbao-admin role_id> secret_id=<openbao-admin secret_id>)
  ```

  AppRole login is `bao write auth/approle/login`, not
  `bao login -method=` — the CLI's `-method` flag has no approle
  handler. The `openbao-admin` creds are ansible-vault'd inline in
  `inventories/prd/group_vars/openbao.yml`; read them back with:

  ```bash
  cd ansible && poetry run ansible srvvault1 -m debug \
      -a 'msg="role_id={{ openbao_admin_role_id }} secret_id={{ openbao_admin_secret_id }}"'
  ```

## 2 — Single-node loss

One `srvvaultN` is gone (PVE host down, disk failure, corruption).
The other two hold quorum, the VIP rides the surviving leader, and
clients are unaffected. Goal: rebuild the node and return it to a
voter.

1. **Recreate the VM.** Terraform refuses to replace a prd VM, so
   destroy it on Proxmox first if it is still there, then apply —
   Terraform's refresh finds it gone and recreates it. Its `vm_id` and
   `pve_node` are under its key in `terraform/prd/vms.tf`:

   ```bash
   ssh root@<pve_node> 'qm stop <vm_id> && qm destroy <vm_id>'
   cd terraform/prd && terraform apply
   ```

2. **Converge the node.** A full run is safe — the two healthy nodes
   reconverge to a no-op:

   ```bash
   cd ansible && poetry run ansible-playbook playbooks/site-openbao.yml
   ```

   `elect-bootstrap.yml` probes all peers, sees the cluster already
   initialized, and routes the rebuilt node through `join.yml` — it
   targets a live initialized peer, so this works even when the
   rebuilt node is `srvvault1`. The leader then streams the Raft
   snapshot and the static seal auto-unseals the node.

   (To converge only the rebuilt node, add
   `--limit '<srvvaultN>,localhost'` — `localhost` must stay in the
   limit or Play 0's known-hosts seeding is skipped.)

3. **Verify.**

   ```bash
   BAO_ADDR=https://srvvaultN.home:8200 bao operator raft list-peers
   ```

   Expect three voters. Confirm the VIP never moved off a surviving
   node (`ip -br addr show` on each), and that a known secret reads
   back on the rebuilt node.

## 3 — Whole-cluster loss

All three nodes are gone simultaneously. The cluster is rebuilt empty
and the latest backup's Raft snapshot is restored into it.

1. **Fetch and unpack the latest backup.** `backup-server` is
   upload-only — pull the object from the rclone destination it
   ships to (`backupServer.rcloneRemote` in StorageDeploy's `config/prd/values.yaml`).
   The newest `openbao/<ts>_openbao-backup.tgz.age` is the one you
   want, not the `.metadata.json` file beside it:

   ```bash
   age -d -i <age-key-from-Roboform> \
       openbao/<ts>_openbao-backup.tgz.age > openbao-backup.tgz
   tar xzf openbao-backup.tgz        # yields raft.snap + *.json
   ```

2. **Rebuild all three VMs.** Destroy each `srvvaultN` still on
   Proxmox (`vm_id` and `pve_node` under its key in
   `terraform/prd/vms.tf`), then apply; Terraform recreates the three:

   ```bash
   ssh root@<pve_node> 'qm stop <vm_id> && qm destroy <vm_id>'   # each srvvaultN still on Proxmox
   cd terraform/prd && terraform apply
   ```

3. **Converge a fresh empty cluster.** Same seal key, so it
   auto-unseals; `srvvault1` initialises, `srvvault2/3` join:

   ```bash
   cd ansible && poetry run ansible-playbook playbooks/site-openbao.yml
   ```

   Capture the fresh root token from the init output — it authorises
   the restore in the next step:

   ```bash
   ssh srvvault1 sudo cat /dev/shm/openbao-init.json
   ```

   This converge leaves the backup pipeline unconfigured. The empty
   cluster has no AppRole auth to prove a staged backup secret_id
   against, so it prints `Backup AppRole secret_id not delivered`, or
   `OpenBao backup pipeline not configured` when none is staged.
   Step 7 delivers it.

4. **Restore the snapshot.** Copy `raft.snap` to `srvvault1`, then:

   ```bash
   BAO_ADDR=https://srvvault1.home:8200 BAO_TOKEN=<fresh root token> \
       bao operator raft snapshot restore raft.snap
   ```

   The restore replaces cluster state with the snapshot's. The fresh
   root token is overwritten in the process; from here authenticate
   as the `openbao-admin` AppRole (step 5). The fresh init file at
   `/dev/shm/openbao-init.json` is now stale — leave it; tmpfs clears
   on reboot and the keys it holds are inert.

   Every AppRole is back with the secret_ids it held at the snapshot.
   A consumer outside OpenBao keeps the secret_id it was last given,
   so a consumer whose AppRole was rotated after the snapshot is
   rejected. Steps 5 to 8 give each rejected consumer a fresh one:
   `openbao-admin` first, because the playbook logs in with it; then
   `iac-agent` by the playbook, because srviac resolves the rotator's
   own credentials through it; then the rest with the rotator. The
   rotator's own pair needs nothing: it lives in
   `kv/iac/rotator-approle`, restored together with its AppRole.

5. **The admin AppRole.** From the Ansible checkout, define `drop`,
   which steps 5 to 8 use, then log in:

   ```bash
   drop() { for a in $(bao list -format=json "auth/approle/role/$1/secret-id" | jq -r '.[]'); do
       bao write "auth/approle/role/$1/secret-id-accessor/destroy" secret_id_accessor="$a"; done; }
   . scripts/bao-login.sh
   ```

   `drop` destroys every secret_id an AppRole holds. Run it only for
   an AppRole whose consumer is rejected: then no consumer holds any
   of them, and the rotator's mint for `openbao-admin`, `iac-agent`,
   `jenkins` and `backup` fails while the role holds more than one.

   `bao-login: BAO_ADDR=…` means the inventory's `openbao-admin`
   secret_id still logs in: go to step 6. `approle login failed`
   means the snapshot predates the admin's last rotation. The
   `openbao` role then skips every auth task without failing, so the
   playbook runs below would deliver nothing. Mint a root token from
   the Shamir recovery keys in Roboform, 3 of 5:

   ```bash
   export BAO_ADDR=https://secrets
   bao operator generate-root -init                  # prints the nonce and the OTP
   bao operator generate-root -nonce=<nonce>         # three times, one recovery key each, at its prompt
   export BAO_TOKEN=$(bao operator generate-root -decode=<encoded token> -otp=<otp>)
   ```

   Drop the admin's, then mint a fresh one straight into the vault
   format:

   ```bash
   drop openbao-admin
   bao write -f -field=secret_id auth/approle/role/openbao-admin/secret-id \
       | (cd ansible && poetry run ansible-vault encrypt_string --stdin-name openbao_admin_secret_id)
   ```

   Replace the `openbao_admin_secret_id: !vault |` block in
   `ansible/inventories/prd/group_vars/openbao.yml` with the printed
   one, commit and push. Then retire the root token and log in as the
   admin:

   ```bash
   bao token revoke -self
   unset BAO_TOKEN && . scripts/bao-login.sh
   ```

   Steps 6 to 8 use `drop` under this admin session.

6. **The `iac-agent` AppRole.**

   ```bash
   ssh ansible@srviac 'sudo iac -c true'
   ```

   Exit 0 means srviac's `OPENBAO_SECRET_ID` still logs in: go to
   step 7. A failed OpenBao login means `iac-agent` was rotated after
   the snapshot. Drop its secret_ids and mint it alone:

   ```bash
   drop iac-agent
   cd ansible && poetry run ansible-playbook playbooks/site-openbao.yml \
       -e openbao_rotate_secret_ids=true -e '{"openbao_rotate_secret_id_roles": ["iac-agent"]}'
   ```

   Put `tmp/openbao-credentials/iac-agent-secret-id` into srviac's
   `/etc/iac/secrets.yaml` as the `OPENBAO_SECRET_ID` literal, wipe
   the staging files with the `shred -u` the run's closing message
   prints, and run `iac -c true` again.

7. **Converge again, to deliver the backup credential.** The rebuilt
   nodes hold no backup secret_id: step 3 had no `backup` AppRole to
   prove one against. This converge authenticates as the
   `openbao-admin` AppRole, re-stages the restored `backup` role_id,
   and logs in with the staged secret_id before installing it:

   ```bash
   cd ansible && poetry run ansible-playbook playbooks/site-openbao.yml
   ```

   The outcome depends on the checkout's `tmp/openbao-backup-secret-id`:

   - **Staged, and the restored role accepts it**: delivered to every
     node, with `openbao-backup.timer` enabled.
   - **Staged, and the restored role rejects it**: the run fails at
     `Refuse a staged backup secret_id the backup AppRole rejects`,
     naming `-e openbao_rotate_secret_ids=true`.
   - **None staged** (a fresh clone, as every Jenkins run is): the
     run completes with `OpenBao backup pipeline not configured`, and
     no timer is installed.

   For either of the last two, drop the `backup` secret_ids (no node
   holds one) and converge once more, minting for `backup` alone:

   ```bash
   drop backup
   cd ansible && poetry run ansible-playbook playbooks/site-openbao.yml \
       -e openbao_rotate_secret_ids=true -e '{"openbao_rotate_secret_id_roles": ["backup"]}'
   ```

   It mints and stages a fresh `backup` secret_id, which every node
   proves and receives. Once the nodes hold it, delete the staged
   copy: the rotator's next `backup` rotation makes it stale, and a
   converge from a checkout holding a stale one fails at its login.

   ```bash
   shred -u tmp/openbao-backup-secret-id
   ```

8. **The rotations made since the snapshot.** The restore undid in
   OpenBao every rotation made after the snapshot, while the
   consumers may hold the newer values. The nightly job's log lists
   them. In each `IaC/Scheduled Secret Rotation` build since the
   snapshot's date, a plan starts with a line
   `── <kind> plan of <leaf> (<keys>) · …`, and a plan that ran ends
   in `rotated`. Add the rotations made by hand since, with
   `secret-rotator run <leaf>`, which no log lists: the vendor tokens
   pasted since are among them, and the store holds their
   predecessors. Run each again, on srviac:

   ```bash
   ssh -t ansible@srviac "sudo iac -c 'secret-rotator run <leaf>'"
   ```

   For an AppRole leaf, `rotator/approle/<role>`, run `drop <role>`
   first: its consumer holds a secret_id the store does not know.
   Steps 5 to 7 have done `openbao-admin`, `iac-agent` and `backup`,
   and `iac/rotator-approle` needs nothing, so skip those.

9. **Verify.**

   ```bash
   export BAO_ADDR=https://secrets
   bao operator raft list-peers      # three voters
   bao secrets list                  # kv/ present
   bao policy list                   # the seven role policies present
   bao kv get kv/<a known path>      # a real secret reads back
   ```

   Run one backup on the node `list-peers` shows as leader:

   ```bash
   ssh srvvaultN sudo systemctl start openbao-backup.service
   ssh srvvaultN sudo journalctl -u openbao-backup -n 5 --no-pager
   ```

   Expect `openbao-backup: backup uploaded (…)`. A failed run names
   the call that broke.

   Each consumer logs in again: `iac -c true` on srviac exits 0
   (iac-agent), ClusterSecretStore `openbao-prd` reads `Valid` (eso),
   a `withVault` build passes (jenkins), and the backup run above
   uploaded (backup).

## 4 — Break-glass: read a secret without a cluster

The backup `.tgz` carries a plaintext `kv.json` alongside the
snapshot. To read one secret when no OpenBao is running — e.g. a
credential needed to bring the cluster itself back:

```bash
age -d -i <age-key-from-Roboform> \
    openbao/<ts>_openbao-backup.tgz.age | tar xzOf - kv.json \
    | jq '."kv/<path>"'
```

This is for reading, not restoring. A full recovery always goes
through the snapshot (§3).

## 5 — Rotation

SecretRotator rotates the secrets of the `kv` mount, by the
annotations of
[`secret-rotation/design.md`](../../../AnsibleSpecs/secret-rotation/design.md)
§5 on each key (below). Every night at 05:30,
`IaC/Scheduled Secret Rotation` runs it on srviac: it rotates each
key that is due, rolls the new value out to its consumers and stamps
the key. Its findings and failures go on one standing YouTrack card
tagged `Secret Rotator`, and to Telegram. Its switches (`dry_run`,
`paused`, `kinds_enabled`, `max_rotations_per_run`) are committed in
SecretRotator's `src/secret_rotator/switches.yaml`. Disabling the job
stops it at once. Bringing it up is
[`secret-rotator-go-live.md`](secret-rotator-go-live.md).

Its commands run on srviac, because its AppRole is bound to srviac's
address. The VS Code tasks `secret-rotator run (srviac)` and
`secret-rotator plan (srviac)` run the last two:

```bash
ssh -t ansible@srviac "sudo iac -c 'secret-rotator audit'"             # the whole mount against the annotation contract
ssh -t ansible@srviac "sudo iac -c 'secret-rotator annotate'"          # dry run: the entries the seed adds or changes, the keys it removes
ssh -t ansible@srviac "sudo iac -c 'secret-rotator annotate --apply'"  # writes them, and creates the marker leaves
ssh -t ansible@srviac "sudo iac -c 'secret-rotator stamp <leaf> <key> --rotated-at <date>'"  # a key's rotation stamp (below)
ssh -t ansible@srviac "sudo iac -c 'secret-rotator plan <leaf>'"       # the leaf's plans: when each falls due, every step
ssh -t ansible@srviac "sudo iac -c 'secret-rotator run <leaf>'"        # runs one of them, its operator steps as prompts
```

`run <leaf>` rotates a key by hand. It is also the only way a plan
with an operator step runs: a `manual` key, and the `iac-agent` and
`openbao-admin` AppRoles. The nightly run starts no such plan; it
marks the leaf manual-due and says so in Telegram. A plan that failed
stays stopped where it failed; `run <leaf>` offers Retry, Abort (roll
back) and Details. A value is typed at a hidden prompt, never on a
command line.

A `manual` key's prompt shows the standard instructions of its
credential type, the `type` in its entry's `args` (SecretRotator's
`src/secret_rotator/kinds/manual/types/<type>.md`), with the key's
`notes` below them. For a type whose credential expires it asks the
new credential's expiry too, `expires on (YYYY-MM-DD, blank for
none)`.

- **Backup upload token** — `terraform taint
  homelab_backup_credential.openbao`, then re-apply Terraform and
  `site-openbao.yml`. The role rewrites `/etc/openbao/backup-token`.
- **AppRole secret-ids** — the rotator's `approle` kind rotates the
  seven AppRoles, from `iac/rotator-approle` for its own and from the
  marker leaves `rotator/approle/<role>` for the rest. It mints a
  secret_id that expires after four times the role's interval, and
  never sooner than 90 days, and records that date as the key's
  `expires_at`. It proves the new secret_id with a login, delivers
  it, then destroys the one the consumer held. Every
  AppRole keeps `secret_id_ttl` `0`. A role's first secret_id, and
  recovery (§3), go through the playbook instead:

  ```bash
  cd ansible && poetry run ansible-playbook playbooks/site-openbao.yml \
      -e openbao_rotate_secret_ids=true -e '{"openbao_rotate_secret_id_roles": ["<role>"]}'
  ```

  It mints a secret_id that never expires, for each role listed (all
  seven without the list), and destroys none. Recapture the staged
  creds per the role README §First-apply procedure. While `jenkins`,
  `backup`, `iac-agent` or `openbao-admin` holds more than one
  secret_id, the rotator's mint for it fails before minting. Once
  every consumer holds its new secret_id,
  `scripts/rotation/accessor_cleanup.py` destroys each accessor that
  no consumer holds: run it dry first, then with `--apply` (its
  header gives the rest).
- **Static seal key** — generate a new key, bump
  `openbao_seal_current_key_id`, and follow the seal-rekey path; the
  old key id must stay declared until every node has migrated.

### Custom metadata: the rotation entries; the run state

OpenBao KV-v2 supports per-path `custom_metadata` — string→string
pairs that travel alongside the secret data but are invisible to
consumers (ESO, the Jenkins Vault plugin, the iac-impl `!bao`
resolver). The rotator keeps the annotations there: one entry per
data key, and nothing else.

**The entries**: `rotation_<key>`, `<key>` the data key's name, a
JSON object of the key's `kind`, `interval` (`14d` when absent),
`args`, `activate`, `expires_at` and `notes` — for example
`{"kind":"random","activate":"auto"}`. A copy's entry holds its
`kind` and `activate`, a `none` key's its `kind`, each with any
`notes`. Their values come from
[`secret-rotation/catalog.md`](../../../AnsibleSpecs/secret-rotation/catalog.md).
`secret-rotator annotate --apply` writes them by metadata patch from
SecretRotator's seed, `src/secret_rotator/seed.yaml`, which is
transcribed from the catalog: a change to a catalog row is made in
the seed too. The apply makes a seed leaf's custom metadata exactly
its entries: it removes every other key, which its dry run lists
first, and sets `max_versions` 20 on a leaf with a key whose kind is
neither `manual` nor `none`. `secret-rotator audit`, and every
nightly run, check the whole mount against the contract; a key
without its entry is a finding on the standing card. Until the
go-live's apply
([`secret-rotator-go-live.md`](secret-rotator-go-live.md) §6), the
live leaves still carry the layout
[`openbao-hygiene-cutover.md`](openbao-hygiene-cutover.md) wrote on
2026-10-04 (`rotation_mechanism`, `rotation_interval`,
`rotation_activate`, `key_<key>`, …), which that apply removes.

A key's `expires_at` is the date its current credential stops
working; the key falls due 7 days before it. Rotations write it:
`approle` the expiry of the secret_id it minted, a `manual` rotation
of a type that expires the date the operator enters, every other
rotation clears it. `annotate` writes the seed's `expires_at` only
when it creates the key's entry.

**The run state** is not on a secret leaf. It is the rotator's own
leaf, `kv/rotator/state`, one JSON object per secret leaf: each
scheduled key's rotation stamp (the date it was last rotated), the
leaf's status, last run, last error, consumers and backoff. The
rotator writes it, by check-and-set. A key is due at its stamp plus
its interval, or 7 days before its `expires_at` when that comes
first; a key with no stamp is due at once. A rotation stamps exactly
the keys it rotated. A key that is a copy or `none` carries no
stamp: a copy is written with its primary. A plan in flight is its
staging leaf, `kv/rotator/staging/<kind>/<leaf>`, which lasts until
the plan is stamped or rolled back.

A key rotated outside the rotator gets its stamp from
`secret-rotator stamp`, after the `bao kv put` of its new value:

```
ssh -t ansible@srviac "sudo iac -c 'secret-rotator stamp <leaf> <key> --rotated-at $(date -uI)'"
```

It prints `<leaf>#<key>: rotation stamp <date>, was <date|none>`.
`--rotated-at` takes the date the value was written, never after
today (UTC). `--expires-at <date>` sets the key's `expires_at`, and
`--clear-expires-at` clears it, for a credential whose expiry
changed outside a rotation; either goes with `--rotated-at` or
alone.

Never use `bao kv metadata put` here. It replaces the leaf's whole
custom metadata, so the leaf loses its entries.

Inspect one leaf with `secret-rotator plan <leaf>`, which says when
each key falls due, or the stamps in bulk:

```
bao kv get -mount=kv -format=json rotator/state \
  | jq '.data.data | map_values(fromjson | .stamps // {})'
```

At go-live no key had a stamp, the values exposed during the
migration among them (Jenkins-credentials-dump, HelmCharts configs,
the iac-impl `secrets.yaml` capture during the runtime-secrets-sweep
slice). The rotator's first pass rotates them, at most
`max_rotations_per_run` a night, kind by kind as each is enabled.

#### A new leaf

A leaf written to the store also needs:

1. a catalog row;
2. its leaf in SecretRotator's `src/secret_rotator/seed.yaml`, and
   its key names in `src/secret_rotator/store-keys.json`, which
   SecretRotator's tests hold the seed to. A push to SecretRotator's
   `main` reaches srviac once its green build has rebuilt the `iac`
   image;
3. `secret-rotator annotate --apply` on srviac, once the leaf exists.

Until the apply has run, the audit reports each of the leaf's keys as
`rotation_<key>: missing`. Its keys have no stamp, so they are due
at once: stamp a freshly minted value with today's date, as above,
unless it should rotate at the next run.

A key's `notes` are freeform context in its entry — what consumer it
serves, the account, scopes or project a re-mint needs. A `manual`
key's prompt shows them below its type's standard instructions. The
audit requires `notes` on a key whose interval is `never`. Change
them in the seed: the apply replaces a live note with the seed's, and
removes one the seed does not carry.

## Consumer cold-boot

Three consumers read from OpenBao at runtime: the iac-agent
container on srviac, the Jenkins controller, and ESO inside each
microk8s cluster. When OpenBao is unreachable (whole-cluster loss
mid-recovery, network partition, listener cert expired), each one
degrades differently. The right intervention depends on which one
is on fire. An expired listener cert is renewed per
[`internal-tls-expiry.md`](internal-tls-expiry.md).

### iac-agent

`iac-impl` resolves every `!bao kv/iac/<leaf>#<key>` reference in
`/etc/iac/secrets.yaml` at container startup, before any iac flow
runs. With OpenBao unreachable the container exits non-zero and
the iac CLI prints the failed path / property.

Escape hatch: edit `/etc/iac/secrets.yaml` on srviac and replace
the failing `!bao` ref with a literal value from Roboform. The
file format accepts a literal where a `!bao` tag was expected, so
the swap is a one-line edit. Restart the iac container, run the
flow, swap back when OpenBao is up.

Full sequence is in [`iac-cold-boot.md`](iac-cold-boot.md).

### Jenkins (HashiCorp Vault plugin)

In-flight pipelines that hit `withVault` block at the resolution
step and fail with the path/property the plugin couldn't read.
Pipelines already past `withVault` keep running on the values they
captured into the closure body. Queued pipelines retry per the
plugin's retry config (default: none — they fail immediately).

Escape hatch: Jenkins → Manage Credentials → System → Global —
re-create the now-deleted Secret-text credential with the same
ID as the `withVault` block's `envVar`. The pipeline reads the
*Secret text* credential at the same env-var name; if it sees one
that matches, it doesn't touch Vault. Restore the value from
Roboform. Delete the credential entry when OpenBao is back.

Tokens already minted by `withVault` (during a successful
pipeline run) have a 1h TTL (`openbao_jenkins_token_ttl`); a
mid-pipeline OpenBao outage past that window kills the next
in-pipeline resolution attempt. Long-running pipelines that need
to span an outage should pre-resolve every secret at the top.

### ESO (External Secrets Operator)

Existing k8s Secrets keep working (ESO caches the last successful
sync). New `ExternalSecret` values stall at
`status.conditions.reason=SecretSyncError`; consumer pods don't
see updates, but the cached Secret content is still served from
the API server. Pods that restart pull the *cached* value, not a
fresh one — that's a footgun on rolling deploys during an outage.

Escape hatches:

1. **Edit the target Secret directly.** Replace the ESO-managed
   data field with the Roboform value, then add
   `external-secrets.io/suspend: "true"` as an annotation on the
   ExternalSecret so ESO doesn't overwrite the edit on the next
   sync attempt. Remove the annotation when OpenBao is back.
2. **Hand-stage Secrets in the namespace** with the same name,
   then delete the ExternalSecret. Re-deploy the chart later to
   restore the ExternalSecret CR.

Bootstrap-tier reminder: each cluster's ESO AppRole `secret_id`
(`eso` on the prd cluster, `eso-dev` on the dev cluster) reaches
ESO via the hand-staged `openbao-eso-approle` Secret in that
cluster's ESO namespace (`external-secrets-prd` on prd) — not via ESO. Losing that
Secret (or rotating the AppRole's secret_id in OpenBao without
re-staging) breaks that cluster's ESO until you re-create it. The
secret_ids live in Roboform under "OpenBao eso AppRole" and
"OpenBao eso-dev AppRole".

## Drill log

Timings from the recovery drills:

- **Single-node loss** — _TBD: record VM rebuild, converge, and
  Raft-join durations from the single-node-loss drill._
- **Whole-cluster loss** (drill of 2026-05-23) — converge of a
  fresh empty cluster took 6m32s; snapshot restore plus end-to-end
  verification (peers, kv read) finished ~5 min after that. Terraform
  rebuild and snapshot fetch/decrypt durations weren't captured this
  round; record them next drill.

## What can go wrong

- **A converge fails at `Refuse a staged backup secret_id the backup
  AppRole rejects`** — the checkout's `tmp/openbao-backup-secret-id`
  is a leftover the live `backup` AppRole no longer accepts, and
  nothing was installed. When the rotator has delivered a `backup`
  secret_id since it was staged, delete the file: the nodes keep the
  one they hold. Otherwise mint one for `backup` alone, as §3 step 7
  does.
- **A nightly backup failed** — once two nights in a row have not
  landed a backup, `BackupOverdue` fires for the `openbao` scope;
  [`backup-freshness.md`](backup-freshness.md) §1 is its triage.
  `journalctl -u openbao-backup` on the node that was leader names
  the call that broke, with its HTTP status and OpenBao's error text.
  `POST auth/approle/login failed: HTTP 400: invalid role or secret
  ID` means the AppRole rejected the nodes'
  backup credentials: deliver a fresh one with
  `secret-rotator run rotator/approle/backup` on srviac (§5). An
  upload that fails with `HTTP 401` has a bad upload token: rotate it
  per §5.
- **Snapshot endpoint returns 403** — the `backup` AppRole policy is
  missing `read` on `sys/storage/raft/snapshot`. Re-apply the role.
  (`read` alone is sufficient — confirmed against OpenBao 2.5.4.)
- **Restore rejected for a seal mismatch** — the rebuilt cluster
  must use the same static seal key as the snapshot. Confirm
  `openbao_seal_current_key_id` and `files/static.key` match what
  was live when the snapshot was taken.
- **VIP duplicated during a partition** — a minority node may briefly
  hold the VIP on its segment. Raft denies writes without quorum, so
  correctness holds; clients on the wrong side just get errors.

## Pre-flight checklist

- [ ] The age private key is in Roboform — needed for every backup
      decrypt.
- [ ] The Shamir recovery keys (3-of-5) are in Roboform — needed to
      mint a root token post-restore.
- [ ] The ansible-vault passphrase is in Roboform — needed for the
      static seal key on every converge.
- [ ] You know the rclone destination path where `backup-server`
      stores the `openbao/` scope.
