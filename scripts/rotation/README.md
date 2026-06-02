# OpenBao secret rotation

Tooling for rotating the homelab's runtime secrets in OpenBao (`kv` mount).
Born out of the `runtime-secrets-sweep` slice: every secret was migrated
into OpenBao from a transcript-exposed source, so all of them need a
fresh value minted at least once ("slice-close rotation"), and we want a
repeatable mechanism for ongoing rotation afterwards.

## The two metadata attributes

Every `kv` leaf carries `custom_metadata` that drives rotation:

| key                  | meaning |
|----------------------|---------|
| `notes`              | freeform sourcing / rotation context |
| `rotated_at`         | ISO date; **present = minted fresh**. Absent = still transcript-exposed, needs rotation. |
| `rotation`           | trust class — see below |
| `rotation_mechanism` | the system/handler used to mint the replacement |

`rotation` (trust class):

- **`unrestricted`** — we mint the value, no other party involved. A
  random string the consumer simply adopts on restart (app session keys,
  signing secrets). Fully scriptable.
- **`coordinated`** — we mint it, but it must be registered in a system
  *we run* (Keycloak, Postgres/MySQL, Elasticsearch, the MQTT broker,
  Ceph, Samba, Jenkins, Home Assistant, Proxmox, hosts' `authorized_keys`)
  — including DB/RabbitMQ, whose engine only reads the password at init,
  and leaves whose value is shared across several leaves.
- **`external`** — a third party mints it (OpenAI, Google, GitHub,
  Mouser, Twitter, TorGuard, …). We fetch and paste; not self-rotatable.

`rotation_mechanism` is what a rotation job dispatches on:
`random`, `postgres`, `rabbitmq`, `keycloak`, `mqtt`, `elasticsearch`,
`kibana`, `ceph-rgw`, `ceph-cephx`, `samba`, `jenkins`, `home-assistant`,
`proxmox`, `ssh`, `wifi`, `dnsmasq`, `backup-server`, `android-keystore`,
`mysql`, `openai`, `google`, `github`, `mouser`, `twitter`, `torguard`,
`third-party-blob`.

The two axes are orthogonal: `rotation` says *how much ceremony*;
`rotation_mechanism` says *which handler*. `unrestricted` ⟺ `random`;
`external` ⟺ a provider mechanism; `coordinated` spans the internal
system mechanisms.

## Scripts

All are read-only against OpenBao except where noted, read **no** secret
values into their output, and depend on nothing in `tmp/`. Each needs a
logged-in `bao` session first:

```sh
export BAO_CACERT=ansible/roles/baseline/files/homelab-root.crt
. scripts/bao-login.sh
```

| script | what it does |
|--------|--------------|
| `annotate.sh` | Classifies every leaf and stamps `rotation` + `rotation_mechanism` (idempotent; preserves `notes`/`rotated_at`; skips strays and refuses to guess on unrecognised leaves). Dry-run by default; `--apply` to write. Re-run when new leaves are added. |
| `audit.sh [out.md]` | Generates the rotation checklist: leaves that still need rotating (no `rotated_at`), **excluding `rotation=unrestricted`**, grouped by `rotation_mechanism`. Read-only. |
| `rotate-unrestricted.sh` | The `random` handler: regenerates every `rotation=unrestricted` leaf with a fresh random value and stamps `rotated_at`. Dry-run by default; `--apply` writes. Restart consumers afterwards so they pick up the new value. |

## Workflow

1. `annotate.sh --apply` — once, and again whenever leaves are added.
2. `rotate-unrestricted.sh --apply` — rotates the stateless app secrets;
   then `kubectl rollout restart` the affected workloads.
3. `audit.sh tmp/rotation-checklist.md` — work the remaining
   (`coordinated` / `external`) leaves by mechanism. Each rotated leaf
   drops off the list once it carries `rotated_at`.

## Per-mechanism rotation (future)

`rotation_mechanism` exists so rotation can be automated handler by
handler. Most `coordinated` mechanisms are scriptable with the right
access — Keycloak admin API (`oidc`/`keycloak-admin`), `ALTER ROLE`
(`postgres`), the ES user API, RabbitMQ mgmt, `radosgw-admin`,
`smbpasswd`. `external` ones stay manual.

**Build them as per-mechanism jobs**, each scoped to one system's admin
creds + an OpenBao policy that can write only that mechanism's leaves
(`rotation_mechanism=<x>` makes the policy query trivial). Avoid a single
cronjob holding admin to everything — it would be the highest-value
target in the homelab. Suggested order: `random` (done) → `keycloak`
(largest clean chunk) → `postgres`/`rabbitmq` last (stateful: `ALTER` +
verify the app reconnects + roll back on failure, and rebuild the derived
`url` key).

## Notes

- Whole-file blobs (`pgpass`, `kibana-config`, `version-poller/config`,
  `mydownloads-config`, `webathome-org-config`, `gluetun-wg`) can't be
  blindly random-rotated — a handler must rebuild the file.
- Some values live in multiple leaves and must rotate together (e.g.
  `dnsmasq/management-api` = `iac/dns-reservation`; `storage/backup-server`
  = `iac/backup-server`; the three identical DB passwords behind
  `pgadmin/pgpass`). The audit output flags these.
- Strays excluded by `is_stray` (not real secrets): `test/nested/leaf`,
  `eso/jenkins-approle`.

See [`runtime-secrets-sweep`](../../../AnsibleSpecs/slices/runtime-secrets-sweep.md)
§Decisions for the rationale behind the taxonomy.
