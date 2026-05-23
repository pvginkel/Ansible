# IaC agent cold-boot runbook

When to use this runbook: the OpenBao cluster is unreachable, or the
homelab is recovering from a whole-cluster loss, and the IaC agent
still needs to run (to bring OpenBao itself back, or to bring up the
rest of the fleet that OpenBao secrets gate).

The IaC agent's `iac-impl` resolves `!bao` references in
`/etc/iac/secrets.yaml` against OpenBao at container startup. When
OpenBao is down, every `!bao` ref hard-fails — by design — and `iac`
won't run. This runbook is the escape hatch: temporarily substitute
literals for every `!bao` ref so `iac` runs without an OpenBao round
trip, do the recovery work, then flip back.

Design context:
[`/work/AnsibleSpecs/slices/completed/iac-secrets-resolver.md`](../../../AnsibleSpecs/slices/completed/iac-secrets-resolver.md),
"Runtime secrets — IaC agent resolver" in
[`/work/AnsibleSpecs/decisions.md`](../../../AnsibleSpecs/decisions.md),
and §Secrets resolver in
[`/work/AnsibleSpecs/phases/completed/openbao.md`](../../../AnsibleSpecs/phases/completed/openbao.md).

## Conventions

- All steps run on `srviac` (or any host with a working `iac` install
  and Docker), as root or via sudo. `wrkdev` works as a fallback if
  `srviac` itself is down — `install.sh` runs on either host.
- "Roboform" means the operator's password manager of record. Every
  literal you'll need is already in there, one entry per secret.
- The OpenBao admin path through the Jenkins agent VM is unavailable
  during cold boot — that's the whole reason you're here. Don't
  invent a half-restore that depends on it.

## What gets substituted

Today, `/etc/iac/secrets.yaml` has two kinds of entries:

| Kind | Example | Cold-boot action |
|---|---|---|
| **Literal** | `value: ghp_xxxxx` | Leave as-is. |
| **!bao ref** | `value: !bao kv/iac/ha-token#token` | Replace with the Roboform-held literal. |

The irreducible-literal set (`OPENBAO_URL`, `OPENBAO_ROLE_ID`,
`OPENBAO_SECRET_ID`, `GIT_API_TOKEN`) is already literal; cold boot
doesn't touch it. Everything else with `!bao` becomes a literal for
the duration of the recovery.

## Procedure

### 1 — Snapshot the current file

```bash
sudo cp /etc/iac/secrets.yaml /etc/iac/secrets.yaml.pre-cold-boot
```

The snapshot is the artifact you'll restore from. Don't skip it.

### 2 — Inventory the `!bao` refs you need to substitute

```bash
sudo grep -nE '^\s*[^#].*!bao ' /etc/iac/secrets.yaml
```

Every match is a ref that needs a Roboform value. The match line
shows the `mount/path#key` triple; Roboform entries are named after
the consumer (e.g. "Home Assistant — long-lived token") rather than
the KV path, so use the surrounding YAML context (the `name:` key on
`env:` entries, the `path:` on `files:` entries) to match up.

### 3 — Substitute literals in place

`sudo nano /etc/iac/secrets.yaml` (or your editor of choice). For
each `!bao` ref:

- **env entry**: replace `value: !bao kv/...` with `value: "<literal>"`.
- **files entry**: replace `content: !bao kv/...` with a folded
  block. Mode lines stay as-is.

Example before:

```yaml
- name: HA_TOKEN
  value: !bao kv/iac/home-assistant#token

- path: /root/.ssh/id_ed25519_ansible
  content: !bao kv/iac/ansible-ssh-key#private
  mode: "0600"
```

Example after:

```yaml
- name: HA_TOKEN
  value: "eyJhbGc...<copied from Roboform>"

- path: /root/.ssh/id_ed25519_ansible
  content: |
    -----BEGIN OPENSSH PRIVATE KEY-----
    <copied from Roboform>
    -----END OPENSSH PRIVATE KEY-----
  mode: "0600"
```

Save the file. Confirm permissions stayed at `0600 root:root`:

```bash
sudo stat -c '%a %U:%G' /etc/iac/secrets.yaml
```

### 4 — Run `iac` against the cold-boot config

```bash
sudo iac -c 'echo cold-boot smoke test passed'
```

If `iac-impl` complains about a still-`!bao`-shaped value, you
missed a ref — go back to step 3. If it complains about anything
else, fix it.

From here, drive recovery via `iac -c '...'` as normal. Examples:

- **Recover OpenBao** — follow `openbao.md` recovery sections (single-
  node loss or whole-cluster loss). Both run via `iac` and need
  ansible-vault content; the vault passphrase is its own irreducible
  literal and is independent of this runbook.

- **Bring up other consumers** — Helm-deployed workloads that read
  from OpenBao via ESO will fail until ESO has a working
  `SecretStore`. The fix is to bring OpenBao back, not to expand
  this runbook.

### 5 — Flip refs back after OpenBao is restored

Once `bao status` reports healthy on the leader and the iac-agent
AppRole is usable, restore the snapshot:

```bash
sudo mv /etc/iac/secrets.yaml.pre-cold-boot /etc/iac/secrets.yaml
sudo stat -c '%a %U:%G' /etc/iac/secrets.yaml   # expect: 600 root:root
```

Smoke-test:

```bash
sudo iac -c 'env | grep -E "^(HA_TOKEN|JENKINS_AGENT_SECRET)" | wc -l'
```

A value of `2` (or however many !bao-resolved env vars you have)
means the resolver is back in service.

### 6 — Rotate any literal that left Roboform

A literal that sat on disk during cold boot has had its blast radius
expanded for the duration. If the cold boot lasted long enough to
matter, rotate the affected secret(s) — generate a new value, write
it under the same KV path, and let the next iac invocation pick it
up. This is a judgment call; for a 30-minute recovery, skip; for a
multi-day outage, do it.

## What can go wrong

- **You misspell a literal**, and the consumer logs an auth error
  that's easy to confuse for OpenBao downtime. The fix is to verify
  the literal in Roboform and re-edit. Don't chase OpenBao.
- **You forget step 5**, and the rest of the fleet runs with stale
  literals after OpenBao is back. Drift cycle won't catch this — the
  resolver only reads `secrets.yaml`, it doesn't audit whether refs
  used to be `!bao`. Set a reminder before you start step 3.
- **You commit `/etc/iac/secrets.yaml`** by mistake. The file is not
  in a git repo, but cold-boot procedures sometimes invite `cp` into
  the wrong directory. If literals leak into git history, rotate.

## Pre-flight checklist

Before the next time you might need this runbook:

- [ ] Every `!bao` ref in `/etc/iac/secrets.yaml` has a Roboform entry
      with a clear name. Drift between the two is what makes cold
      boot slow.
- [ ] The operator's age private key (for OpenBao backup decrypt) is
      in Roboform — separate from this runbook, but the same trip to
      Roboform.
- [ ] The ansible-vault passphrase is in Roboform under the
      "operator workstation" entry — needed for the static seal key
      during OpenBao recovery.
