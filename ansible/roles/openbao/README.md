# `openbao` role

Stands up an OpenBao node as a member of a 3-node integrated-Raft
cluster (`srvvault1/2/3`). On first apply against a greenfield cluster
the elected bootstrap node (lowest-sorted member, i.e. `srvvault1`)
runs `bao operator init`; the other two then Raft-join. Later applies
are no-ops on an already-initialized cluster.

Design context:
[`/work/AnsibleSpecs/phases/completed/openbao.md`](../../../../AnsibleSpecs/phases/completed/openbao.md).

## What it does

1. **Elect** a bootstrap candidate by hostname sort; probe each peer's
   `/v1/sys/health` to learn whether the cluster is already
   initialized. The cluster-init signal is per-host but every host
   reaches the same answer (it polls the network, not local state),
   so a rebuilt bootstrap candidate doesn't re-init on top of a live
   cluster — it falls through to the join path (card #9) instead.
2. **Install** the pinned OpenBao `.deb` from GitHub releases, sha256-
   verified before `apt install`. The OpenBao project does not
   maintain an apt repository, so upgrades are Ansible-driven: bump
   `openbao_version` and `openbao_deb_sha256` together, next drift
   cycle picks it up.
3. **Issue** a homelab-CA TLS leaf for the listener via the
   [`internal_tls`](../internal_tls/README.md) role. SANs cover the
   node's short hostname, its `.home` FQDN, and the shared VIP
   `secrets.home`. Reload handler SIGHUPs the openbao process.
4. **Render** `/etc/openbao/openbao.hcl` (Raft storage, TLS listener,
   `seal "static"`, `api_addr` / `cluster_addr`) and drop the
   ansible-vault'd static seal key at `/etc/openbao/seal/static.key`.
5. **Start** the service; wait for `/v1/sys/health` to be reachable.
6. **Initialize** the cluster (`bao operator init`) on the bootstrap
   node, but only when no peer reports initialized. Captures the root
   token + Shamir recovery keys to `/dev/shm` for the operator to
   transfer to Roboform.
7. **Join** the cluster from each non-bootstrap node, but only when
   the cluster is initialized and the local node hasn't already
   joined (sys/health still reports `initialized: false`). The
   static seal auto-unseals each follower once the leader has
   streamed the Raft snapshot to it.
8. **Port-front** the listener via [`haproxy`](../haproxy/README.md) in
   `mode tcp` so clients reach the cluster on `https://secrets/` (edge
   port 443 → local `127.0.0.1:8200`). HAProxy does **not** terminate
   TLS — the per-node `internal_tls` cert is forwarded straight to the
   client, so a follower upgrade or VIP migration doesn't need cert
   synchronisation.
9. **Bind the VIP** via [`keepalived`](../keepalived/README.md) in
   leader-tracking mode against `homelab_vips.openbao`. A `vrrp_script`
   polls `/v1/sys/leader` every 2s; only the Raft leader's check
   passes, raising its effective priority above the followers so the
   VIP (`secrets.home`) follows leadership. Failover ~4 s.
10. **Layer a systemd hardening drop-in** on top of the .deb's
    bundled `openbao.service`. Conservative Protect*/Restrict* set
    that doesn't intersect with OpenBao's HTTPS listener / file
    audit / Raft requirements; see
    [`templates/openbao-hardening.conf.j2`](templates/openbao-hardening.conf.j2)
    for the rationale.
11. **Provision auth** on the bootstrap node — enable approle, write
    the kv-v2 mount, render and write policies (`openbao-admin`,
    `iac-agent`, `jenkins`, `eso`, `backup`), write the AppRoles bound
    to each. Optionally rotate secret-ids
    (`-e openbao_rotate_secret_ids=true`) and retire the root token
    (`-e openbao_retire_root_token=true`).
    Gated on a controller token: `openbao_admin_token` (operator-
    supplied) or an admin AppRole login via vault'd
    `openbao_admin_role_id` / `_secret_id`. Skips cleanly when neither
    is configured so the drift cycle no-ops before first provisioning.
12. **Enable the file audit device** at
    `/var/log/openbao/audit.log` via an `audit "file"` stanza in
    `openbao.hcl`. OpenBao 2.5 rejects the API enable path; audit
    devices must be declarative. The parent directory is created
    by `dirs.yml` ahead of the config render so the daemon can open
    the log on first restart.
13. **Configure ufw** on each node with the documented allow-list
    (22/tcp from `srviac`, 443/tcp from `k8s_prd` + `srviac`,
    8200/tcp from peers + `srviac`, 8201/tcp + VRRP from peers).
    Rules are inserted unconditionally; ufw is only `state: enabled`
    when `openbao_ufw_enable` is `true` (default `false`).
14. **Run a daily backup** on each node via a leader-guarded systemd
    timer. On the Raft leader only, the wrapper authenticates with the
    `backup` AppRole, assembles a `.tgz` (a native Raft snapshot plus
    a plaintext JSON export of policies, auth methods, mounts, and the
    KV-v2 tree), and POSTs it to the in-cluster backup-server;
    followers exit 0. Self-skips until the `backup` AppRole creds and
    the upload token are both available — see §Backup pipeline below.

## Inputs

Required (the role asserts these):

- `openbao_seal_current_key_id` — permanent identifier for the bytes
  in `roles/openbao/files/static.key`. Bump whenever the key file
  changes. Suggested scheme: `YYYYMMDD-N`. Set in
  `inventories/prd/group_vars/openbao.yml`.

Optional (defaults in [`defaults/main.yml`](defaults/main.yml)):

- `openbao_version` — release tag (default pinned in defaults).
- `openbao_deb_sha256` — checksum for the pinned `.deb`.
- `openbao_san_list` — listener SANs. Defaults cover short hostname,
  `.home` FQDN, and the VIP in both `secrets` / `secrets.home` forms
  so the bare `https://secrets/` URL HAProxy fronts validates against
  the same per-node cert HAProxy passes through.
- `openbao_recovery_shares` / `openbao_recovery_threshold` — Shamir
  shape for the recovery keys. Defaults 5/3.

Auth + audit + ufw inputs (cards #40 / #11):

- `openbao_admin_token` — operator-supplied (via `-e`) on the first
  apply (root token from init) and any rescue run. Leave unset for
  steady-state — the role logs in via the admin AppRole instead.
- `openbao_admin_role_id` / `openbao_admin_secret_id` — admin AppRole
  credentials. Vault these into `group_vars/openbao.yml` after the
  first apply prints them.
- `openbao_iac_agent_kv_paths` / `openbao_jenkins_kv_paths` /
  `openbao_eso_kv_paths` — KV-v2 read paths granted to each
  consumer's policy. Empty list = inert policy (AppRole exists, reads
  return 403 until a path is added). Extend per migrated ref.
- `openbao_rotate_secret_ids` — when `true`, mints + prints fresh
  secret-ids for every AppRole (admin, iac-agent, jenkins, eso,
  backup). Default `false`; flip on the first apply and whenever you
  rotate.
- `openbao_retire_root_token` — when `true` and
  `openbao_admin_token` is the root token, revokes it via
  revoke-self. One-shot; set only once admin AppRole creds are in
  vault.
- `openbao_ufw_enable` — `false` by default (rules written, ufw not
  activated). Flip to `true` to lock the host down to the allow-list
  in `tasks/ufw.yml`. Doing so closes wrkdev's SSH path to
  `srvvaultN`; future runs must come through srviac/Jenkins.

Backup pipeline inputs (card #12):

- `openbao_backup_server_url` — in-cluster backup-server base URL
  (default `https://backup-server.home`); the daily backup POSTs here.
- `openbao_backup_oncalendar` / `openbao_backup_randomized_delay` —
  systemd `OnCalendar` + `RandomizedDelaySec` for the timer. Defaults
  fire before the staggered unattended-reboot windows.
- `openbao_backup_staging_dir` — controller-side directory the three
  backup inputs are staged through (default: the playbook's `tmp/`).
  See §Backup pipeline.

The pipeline takes no group_vars/vault input of its own: it consumes
the `backup` AppRole creds (provisioned by the auth tasks) and the
backup-server upload token captured by `site-openbao.yml` Play 0 from
`terraform output openbao_backup_token`.

## First-apply procedure (cards #40 / #11)

Run-once, operator-driven, with the bootstrap-captured root token.
Two applies separate the provisioning from the retire so the operator
can capture the admin AppRole creds into vault between them.

1. **Provision auth + mint creds:**

   ```
   cd ansible && poetry run ansible-playbook playbooks/site-openbao.yml \
       --ask-vault-pass --diff \
       -e openbao_admin_token=<root token from Roboform> \
       -e openbao_rotate_secret_ids=true
   ```

   The `approle.yml` task prints role_ids + secret_ids for
   `openbao-admin`, `iac-agent`, `jenkins`, `eso`, and `backup` at the
   end of the play. Capture them before the buffer scrolls off —
   secret-ids are not recoverable from OpenBao. The `backup` creds are
   the exception: `backup.yml` delivers them straight to each node, so
   there is nothing to capture for that one.

2. **Vault the admin AppRole creds** into
   `inventories/prd/group_vars/openbao.yml`:

   ```
   ansible-vault encrypt_string --name openbao_admin_role_id   '<from step 1>'
   ansible-vault encrypt_string --name openbao_admin_secret_id '<from step 1>'
   ```

   Commit. The drift cycle now authenticates via the admin AppRole.

3. **Paste the iac-agent creds** into `srviac:/etc/iac/secrets.yaml`
   (`OPENBAO_ROLE_ID`, `OPENBAO_SECRET_ID`). Paste the jenkins and
   eso creds into their respective consumer configs (Jenkins Vault
   plugin, ESO SecretStore CR).

4. **Retire the root token:**

   ```
   poetry run ansible-playbook playbooks/site-openbao.yml \
       --ask-vault-pass --diff \
       -e openbao_admin_token=<root token> \
       -e openbao_retire_root_token=true
   ```

   This revokes the root token via `revoke-self`. Delete the
   Roboform entry afterwards; the recovery keys (Shamir 3-of-5) can
   mint a new root token if needed.

5. **Flip ufw on** when ready — see the next section.

## Locking down with ufw

`openbao_ufw_enable` defaults to `false`: the role inserts the allow
rules on every apply but never activates ufw. To lock the host down:

```
poetry run ansible-playbook playbooks/site-openbao.yml --diff \
    -e openbao_ufw_enable=true
```

**Consequence**: 22/tcp gets locked to `srviac` only. Future
`ansible-playbook` runs from `wrkdev` will fail to reach `srvvaultN`;
all OpenBao management must flow through `iac` on `srviac` (the
Jenkins-driven pipelines or interactive `iac` from VSCode Remote-SSH
into the Jenkins agent VM). Disable ufw out-of-band (`ufw disable`)
to break-glass.

## Backup pipeline (card #12)

A daily systemd timer on each node runs a leader-guarded backup to
the in-cluster backup-server — a `.tgz` bundling a native Raft
snapshot (the restore artifact) and a plaintext JSON export (KV tree,
policies, auth methods, mounts) for break-glass reads. Bring it
online after the cluster is provisioned:

1. **Mint the upload credential** (operator runs Terraform):

   ```
   cd terraform/prd && terraform apply
   ```

   This creates `homelab_backup_credential.openbao` and exposes the
   scope-bound token as the `openbao_backup_token` output.

2. **Provision the backup AppRole + deploy the timer:**

   ```
   cd ansible && poetry run ansible-playbook playbooks/site-openbao.yml \
       --diff -e openbao_rotate_secret_ids=true
   ```

   Play 0 stages the token; `approle.yml` mints the `backup` secret-id
   and stages it with the role_id; `backup.yml` delivers all three to
   `/etc/openbao/` on each node and enables `openbao-backup.timer`. The
   rotate flag is required only on this first run — it is what mints
   the `backup` secret-id.

Steady-state applies need no flag: the role_id re-delivers
idempotently, the secret-id file persists, the timer stays enabled.
To rotate the upload token, `terraform taint
homelab_backup_credential.openbao` then re-apply both steps.

**Credential transport.** The three inputs reach each node through
controller-side staging files in `openbao_backup_staging_dir` (the
playbook's `tmp/`): `approle.yml`, on the bootstrap host, writes the
`backup` AppRole `role_id` every provisioning run and the `secret_id`
on rotation runs; `site-openbao.yml` Play 0 writes the upload token.
`backup.yml` on every node reads them back. The handoff cannot use
`hostvars` — the values are `no_log`, and ansible-core 2.20 no longer
exposes `no_log` data across hosts. The staging files persist: they
are the rendezvous between the bootstrap host's converge and the
later `serial: 1` batches, and they keep `backup.yml` evaluable under
a drift `--check`. `tmp/` is gitignored.

## Bootstrap procedure

One-time, operator-driven, before this role's first apply. Mirrored in
the phase-2 doc §Bootstrap procedure.

1. **Generate the seal key** off the controller, on a host the
   operator trusts (operator workstation is fine):

   ```bash
   openssl rand -out /tmp/openbao-static.key 32
   ```

2. **Encrypt with ansible-vault** and copy the passphrase to Roboform:

   ```bash
   ansible-vault encrypt /tmp/openbao-static.key
   cp /tmp/openbao-static.key /work/Ansible/ansible/roles/openbao/files/static.key
   shred -u /tmp/openbao-static.key   # original cleartext gone
   ```

3. **Set the key identifier** in
   `inventories/prd/group_vars/openbao.yml`:

   ```yaml
   openbao_seal_current_key_id: "20260521-1"
   ```

4. **Wire the role into the playbook** —
   `playbooks/site-openbao.yml`'s converge play has a placeholder
   comment between `managed_filesystems` and `ssh_host_cert` where the
   role include goes.

5. **Commit** all of (2)–(4) in one go. This is the only window where
   the cleartext seal key exists outside Roboform's passphrase before
   the role takes over.

6. **First apply** (operator runs):

   ```
   cd ansible && poetry run ansible-playbook playbooks/site-openbao.yml --ask-vault-pass --diff
   ```

   On `srvvault1` the role inits the cluster; `srvvault2` and
   `srvvault3` then Raft-join (the converge play runs `serial: 1`,
   so srvvault2/3's elect-bootstrap probe sees srvvault1 already
   initialized and routes through `tasks/join.yml`).

7. **Capture** the init output into Roboform — root token + 5 recovery
   keys — and delete the staged file:

   ```bash
   ssh srvvault1 sudo cat /dev/shm/openbao-init.json
   ssh srvvault1 sudo rm  /dev/shm/openbao-init.json
   ```

8. **Verify auto-unseal across a reboot** of `srvvault1` before card
   #9 brings the other two in. After reboot, `bao status` (with
   `BAO_ADDR=https://srvvault1.home:8200`) reports `Sealed: false`
   without any operator action.

## Notes

- **disable_mlock = true.** The .deb's systemd unit doesn't grant
  `CAP_IPC_LOCK` and sets `MemorySwapMax=0` (swap off at the cgroup
  level), and OpenBao's integrated-storage guidance recommends
  `disable_mlock = true` for the same reasons. The role's HCL
  template sets it explicitly.

- **Seal-key ownership.** The phase doc and the
  [`openbao-static-seal`](../../../../AnsibleSpecs/slices/completed/openbao-static-seal.md)
  slice pin the seal key at `root:openbao 0440`. This role installs
  it as `openbao:openbao 0400` instead — the .deb postinst runs
  `chown --recursive openbao:openbao /etc/openbao` on every
  install/upgrade, so the slice's ownership would drift on every
  dpkg run. `0400 owner=openbao` is equally tight (only the openbao
  process reads it; root can read anything) and stays stable.

- **Upgrade flow.** Bump `openbao_version` + `openbao_deb_sha256`
  together; the next `iac-scheduled-drift` cycle downloads the new
  `.deb`, sha256-verifies, and reinstalls. The bundled systemd unit
  restarts on `state: present` only when the package version actually
  changes; config/cert/seal-key drift triggers their own handlers.
