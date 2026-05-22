# OpenBao operator runbook

Day-to-day administration and disaster recovery for the homelab
OpenBao cluster. Read this when you need to reach the cluster as an
admin, recover a lost node, recover the whole cluster, or read a
secret out of a backup.

Design context:
[`../../../AnsibleSpecs/phases/openbao.md`](../../../AnsibleSpecs/phases/openbao.md),
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
- **Root token**: retired (card #11). Mint a fresh one from the
  Shamir recovery keys with `bao operator generate-root` when an
  admin path outside the `openbao-admin` AppRole is needed.
- **Convergence playbook**: `playbooks/site-openbao.yml` — runs
  bootstrap → baseline → managed_filesystems → openbao →
  ssh_host_cert against the `openbao` group.

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
  bao login -method=approle \
      role_id=<openbao-admin role_id> secret_id=<openbao-admin secret_id>
  ```

  The `openbao-admin` creds are ansible-vault'd in
  `inventories/prd/group_vars/openbao.yml` — decrypt with
  `ansible-vault view` to read them.

## 2 — Single-node loss

One `srvvaultN` is gone (PVE host down, disk failure, corruption).
The other two hold quorum, the VIP rides the surviving leader, and
clients are unaffected. Goal: rebuild the node and return it to a
voter.

1. **Recreate the VM.** Find the resource address and replace it:

   ```bash
   cd terraform/prd && terraform state list | grep -i srvvault
   cd terraform/prd && terraform apply -replace='<srvvaultN address>'
   ```

2. **Converge the node.** A full run is safe — the two healthy nodes
   reconverge to a no-op:

   ```bash
   cd ansible && poetry run ansible-playbook playbooks/site-openbao.yml \
       --ask-vault-pass --diff
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
   ships to (see the `storage` chart config in `/work/HelmCharts`).
   The newest `openbao/` object is the one you want:

   ```bash
   age -d -i <age-key-from-Roboform> \
       openbao/<ts>_openbao-backup.tgz.age > openbao-backup.tgz
   tar xzf openbao-backup.tgz        # yields raft.snap + *.json
   ```

2. **Rebuild all three VMs.**

   ```bash
   cd terraform/prd && terraform apply \
       -replace='<srvvault1>' -replace='<srvvault2>' -replace='<srvvault3>'
   ```

3. **Converge a fresh empty cluster.** Same seal key, so it
   auto-unseals; `srvvault1` initialises, `srvvault2/3` join:

   ```bash
   cd ansible && poetry run ansible-playbook playbooks/site-openbao.yml \
       --ask-vault-pass --diff
   ```

   Capture the fresh root token from the init output — it authorises
   the restore in the next step:

   ```bash
   ssh srvvault1 sudo cat /dev/shm/openbao-init.json
   ```

4. **Restore the snapshot.** Copy `raft.snap` to `srvvault1`, then:

   ```bash
   BAO_ADDR=https://srvvault1.home:8200 BAO_TOKEN=<fresh root token> \
       bao operator raft snapshot restore raft.snap
   ```

   The restore replaces cluster state with the snapshot's. The fresh
   root token is overwritten in the process; from here authenticate
   with the `openbao-admin` AppRole (its creds are in the snapshot,
   unchanged) or mint a root token from the Shamir recovery keys.

5. **Verify.**

   ```bash
   export BAO_ADDR=https://secrets
   bao operator raft list-peers      # three voters
   bao secrets list                  # kv/ present
   bao policy list                   # the five role policies present
   bao kv get kv/<a known path>      # a real secret reads back
   ```

   Consumers (iac-agent, Jenkins, ESO) need **no** credential
   redistribution — their AppRole `role_id`/`secret_id` pairs are
   part of the restored state.

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

- **Backup upload token** — `terraform taint
  homelab_backup_credential.openbao`, then re-apply Terraform and
  `site-openbao.yml`. The role rewrites `/etc/openbao/backup-token`.
- **AppRole secret-ids** — re-run `site-openbao.yml` with
  `-e openbao_rotate_secret_ids=true`; recapture the printed creds
  per the role README §First-apply procedure.
- **Static seal key** — generate a new key, bump
  `openbao_seal_current_key_id`, and follow the seal-rekey path; the
  old key id must stay declared until every node has migrated.

## Drill log

Timings from the recovery drills (cards #13 / #14):

- **Single-node loss** — _TBD: record VM rebuild, converge, and
  Raft-join durations from the card #13 drill._
- **Whole-cluster loss** — _TBD: record rebuild, converge, snapshot
  fetch/decrypt, and restore durations from the card #14 drill._

## What can go wrong

- **Snapshot endpoint returns 403** — the `backup` AppRole policy is
  missing `read` on `sys/storage/raft/snapshot`. Re-apply the role;
  if 2.5 marks the path root-protected, the policy needs `sudo` too.
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
