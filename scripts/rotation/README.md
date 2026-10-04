# OpenBao rotation tools

Two tools, both run in the dev container (Python with PyYAML):

- `annotate.py` writes the rotation annotations of every leaf in the `kv` mount from one seed,
  and checks the whole mount against the annotation contract.
- `accessor_cleanup.py` destroys the AppRole secret_id accessors that no consumer holds
  ([below](#destroying-stale-secret_id-accessors)).

`kc project test` runs their tests offline (`python3 -m unittest discover -s scripts/rotation`).

The annotation contract is
[`secret-rotation/design.md`](../../../AnsibleSpecs/secret-rotation/design.md) §4 (keys, kinds and
copies in §3.1, per-key intervals in §3.2, activators in §3.3, kinds in §5). The per-leaf values
come from [`secret-rotation/catalog.md`](../../../AnsibleSpecs/secret-rotation/catalog.md). The
rotator (slice 045) reads these annotations.

## Running it

Every live run needs a logged-in session. The offline check needs none:

```sh
. scripts/bao-login.sh
scripts/rotation/annotate.py                  # dry run: per leaf, the metadata keys it would add or change
scripts/rotation/annotate.py --apply          # writes them
scripts/rotation/annotate.py --check          # the contract over the whole kv mount
scripts/rotation/annotate.py --check --keys keys.json   # offline: the seed over keys.json's key names
```

`--seed FILE` replaces the default seed, `seed.yaml` beside the script. `keys.json` maps each leaf
path to the list of its data key names. The exit status is 0 when the apply finished or the check
found nothing, 1 otherwise, and 2 on a usage error. No output carries a secret value: the check
reads each leaf's data only to learn its key names.

## The seed

A YAML mapping from leaf path (under the mount, no `kv/` prefix) to the metadata keys that leaf
gets. A seed may hold only the operator's keys: `rotation_mechanism`, `rotation_interval`,
`rotation_activate`, `rotation_args`, `rotation_expires_at`, `notes`, `key_<name>` and
`interval_<name>`. Every value is a string. A leaf holds at most 64 keys, a key at most 128 bytes
and a value at most 512 bytes. A leaf or key given twice is an error. On any problem the seed is
rejected whole, before OpenBao is contacted.

```yaml
eso/prd/jenkins-telegram-bot/prd/config:
  rotation_mechanism: jenkins-token
  key_telegram-bot-token: manual
  key_telegram-chat-id: none
  rotation_interval: 14d
  interval_telegram-bot-token: 365d
  rotation_activate: auto
```

`seed.yaml` is transcribed from the catalog, so a change to a catalog row is made in the seed too.
`store-keys.json` lists the data key names of every leaf the seed covers, never their values.
`test_seed.py` runs the offline check of the seed over that file, and requires both files to name
the same leaves.

### A new leaf

A leaf written to the store also needs:

1. a catalog row;
2. its entry in `seed.yaml`;
3. its key names in `store-keys.json`;
4. an `annotate.py --apply` once it exists.

Until the apply has run, the check reports the leaf.

## The apply

- A dry run unless `--apply` is given. It reads metadata only.
- It writes with `PATCH kv/metadata/<leaf>`, which is what `bao kv metadata patch` sends. It never
  uses `put` and never writes data. Keys the seed does not name stay as they are: the sweep's
  `rotation`, `rotated_at`, and the rotator's `rotator_*`.
- Seed `notes` that differ from a leaf's existing notes are written first, and the existing text
  follows after ` | earlier: `.
- A second apply changes nothing.
- A seed leaf the store lacks is reported and skipped. A live leaf the seed lacks is reported. The
  check catches both.
- The writes need the `patch` capability on the KV mount. The `site-openbao.yml` converge grants it
  to the `openbao-admin` policy. If OpenBao refuses a write, the run stops at that leaf and says how
  many leaves it patched. Run the apply again after the converge.

## The check

The check walks the whole mount. It prints one line per finding, as `<leaf>: <key>: <what>`, where
the key is the metadata key or data key at fault. A finding is any of these:

- `rotation_mechanism` or `rotation_activate` is missing. `rotation_interval` is missing on a
  leaf with a key that is neither a copy nor `none`.
- A kind in `rotation_mechanism` or in a `key_<name>` is not a kind of design §5, `none`, or
  `copy:<path>#<key>`.
- A data key that its leaf's kind does not own, and that no `key_<name>` names.
- A `key_<name>` or `interval_<name>` names a key the leaf does not have.
- A copy's primary leaf or primary key does not exist.
- A `rotation_interval` or `interval_<name>` is not `<n>d` or `never`, or is `never` while the
  leaf has no `notes`.
- An `interval_<name>` is set on a key whose kind is a copy or `none`.
- A `rotation_activate` is not `auto`, `none`, or a comma list of design §3.3's activators. `auto`
  and `none` stand alone. A `k8s-rollout:` target may be followed by further
  `<ns>/<kind>/<name>` targets.
- A `rotation_args` is not JSON, or is larger than 512 bytes.
- A `rotation_expires_at` is not a date in the form `YYYY-MM-DD`.
- A leaf whose current version is deleted, so its keys cannot be read.

Which keys a kind owns is set in one place, `OWNS` in `annotate.py`:

| Kind | Owns |
|---|---|
| `random`, `manual` | every key |
| `keycloak-client` | `client_secret`; `client_id` is `none` without an override |
| `cnpg-role`, `elastic-user` | `password` |
| every other kind | the leaf's one key that no `key_<name>` names; with several such keys, none of them |

## A rotation done by hand

After writing the new value, stamp the leaf with `kv metadata patch`:

```sh
bao kv metadata patch -mount=kv -custom-metadata=rotated_at="$(date -I)" <leaf>
```

Never use `bao kv metadata put`: it replaces the leaf's whole custom metadata, so every
annotation is lost.

## Destroying stale secret_id accessors

A rotation run of the `openbao` role (`-e openbao_rotate_secret_ids=true`) mints a new secret_id
for every AppRole and destroys none, so every earlier secret_id stays valid. `accessor_cleanup.py`
destroys them once no consumer holds them:

```sh
. scripts/bao-login.sh
scripts/rotation/accessor_cleanup.py                          # dry run: the plan for every AppRole
scripts/rotation/accessor_cleanup.py --apply                  # destroys what the plan names
scripts/rotation/accessor_cleanup.py --role eso-dev --apply   # one AppRole; --role repeats
```

For each AppRole, the tool reads the secret_id that each consumer holds and looks up its accessor.
It then plans to destroy every other accessor of that AppRole. It never guesses. It leaves an
AppRole untouched when it cannot read one of its consumers, or when a consumer holds a secret_id
that OpenBao does not find.

| AppRole | Where its consumers' secret_ids are read |
|---|---|
| `openbao-admin` | the ansible-vaulted `openbao_admin_secret_id` |
| `iac-agent` | the `OPENBAO_SECRET_ID` literal in srviac's `/etc/iac/secrets.yaml` |
| `jenkins` | every Vault AppRole credential in Jenkins with the AppRole's role_id, read through the script console as `JENKINS_USER` with the API token `JENKINS_TOKEN` (an administrator) at `JENKINS_URL` |
| `eso`, `eso-dev` | every ESO SecretStore or ClusterSecretStore with the AppRole's role_id on the prd or dev cluster, read through `~/.kube/config-prd-write` or `~/.kube/config-dev-write`. `eso-dev` needs the dev cluster up. |
| `backup` | `/etc/openbao/backup-secret-id` on every host of the inventory group `openbao` |

For each AppRole, the output is one of two forms:

- `<role>: proven`, then a `keep` line per accessor a consumer holds (with its creation time and
  which consumer holds it), then a `destroy` line per other accessor;
- `<role>: untouched: <reason>`.

A count line follows. `--apply` prints the whole plan before it destroys anything. It then prints
`destroyed <role> <accessor>` for each destroy, and stops at the first destroy that fails. The exit
status is 0 when every selected AppRole was proven (and, with `--apply`, cleaned). It is 1 when an
AppRole was left untouched or a destroy failed, and 2 on a usage error.

The proof covers the secret_id a consumer stores, not the one a running process loaded at start.
After an apply, check that every consumer still logs in.

`--jenkins-fallback` needs `--apply` and the `jenkins` AppRole. If the script console cannot be
read, the tool mints one fresh `jenkins` secret_id and shows it once on the terminal, so that the
operator can paste it into the Jenkins credential. It destroys the other `jenkins` accessors only
after the operator confirms that a `withVault` build passed with the new one. That secret_id, shown
on `/dev/tty` only, is the one secret the tool ever shows. No secret_id reaches stdout, stderr, a
file or a command line.
