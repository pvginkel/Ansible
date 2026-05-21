# `openbao` role

Stands up an OpenBao node as a member of a 3-node integrated-Raft
cluster (`srvvault1/2/3`). On first apply against a greenfield cluster
the elected bootstrap node (lowest-sorted member, i.e. `srvvault1`)
runs `bao operator init`; the other two then Raft-join. Later applies
are no-ops on an already-initialized cluster.

Design context:
[`/work/AnsibleSpecs/phases/openbao.md`](../../../../AnsibleSpecs/phases/openbao.md).

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
  [`openbao-static-seal`](../../../../AnsibleSpecs/slices/openbao-static-seal.md)
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
