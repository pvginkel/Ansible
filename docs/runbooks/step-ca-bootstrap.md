# step-ca bootstrap and lifecycle runbook

Authoritative procedure for everything to do with the homelab CA that
cannot or should not be automated:

- [Day-zero ceremony](#day-zero-ceremony) — generate root + intermediate,
  configure ACME and JWK provisioners, export the root cert, hand the
  CA's material to the `step_ca` role.
- [Enabling the SSH host CA](#enabling-the-ssh-host-ca) — extend the CA
  to also issue SSH host certificates, for the `ssh_host_cert` role.
- [Windows trust install](#windows-trust-install) — one-shot per Windows
  machine the operator uses.
- [Intermediate rotation](#intermediate-rotation) — when the intermediate
  is compromised or routinely rotated. Rotating the **root** is a
  different scope and cadence:
  [`step-ca-root-rotation.md`](step-ca-root-rotation.md).
- [JWK provisioner password rotation](#jwk-provisioner-password-rotation)
  — when the fleet-wide JWK password is rotated.
- [Monitoring smoke test](#monitoring-smoke-test) — verify cert-expiry
  metrics + alert plumbing after the slice lands.

Design context lives in
[`/work/AnsibleSpecs/slices/internal-tls-step-ca.md`](../../../AnsibleSpecs/slices/internal-tls-step-ca.md)
and the "Internal TLS / homelab CA" section of
[`/work/AnsibleSpecs/decisions.md`](../../../AnsibleSpecs/decisions.md).
This runbook is the *operational* path; it doesn't re-justify decisions.

## Conventions

- All ceremony commands run from `wrkdev`. The `step` CLI must be
  installed (`step version` should print without error).
- The root key never leaves Roboform after step 2 of the ceremony.
  Treat any prompt that would write it back to disk in plaintext as a
  bug in this runbook and stop.
- "Roboform" below means the operator's password manager of record.
  Each secret gets its own entry with a descriptive name; copy-paste,
  don't screenshot.
- Working directory during the ceremony is a fresh `mktemp -d`, not the
  Ansible or deploy repo checkouts. Nothing the ceremony produces ends up
  in git except public certificates and, ansible-vault'd, the `step_ca`
  role's files (step 7).
- **step-ca's material is the Ansible repo's `step_ca` role.** Its files
  in `ansible/roles/step_ca/files/` (the table in day-zero step 7) hold
  the CA's keys, their passwords, its certificates, `ca.json` and
  `defaults.json`, the secret ones ansible-vault'd whole-file.
  `ansible/playbooks/step-ca.yml` writes them into the five Secrets in
  `step-ca-prd` and restarts step-ca when a Secret's data changed, since
  step-ca reads them at process start only. So a procedure below that
  changes the CA's material changes a file there and runs that playbook,
  from `~/source/Ansible/ansible`:

  ```sh
  poetry run ansible-vault edit roles/step_ca/files/ca.json   # a vaulted file
  poetry run ansible-playbook playbooks/step-ca.yml
  ```

  Both decrypt with the passphrase `ANSIBLE_VAULT_PASSWORD_FILE` points
  at ([`operator-workstation.md`](operator-workstation.md#ansible-vault-passphrase)).
  The playbook reaches the cluster over SSH to a `k8s_prd` node, through
  the microk8s primary's node kubeconfig. With `--check` appended it
  names the Secrets a run would change, without their data. Commit and
  push the files once the procedure has verified: a run from any other
  checkout writes that checkout's files. StepCaDeploy carries none of
  this material.

---

## Day-zero ceremony

One-shot. Re-running any step short of "rotate intermediate" is a
deviation — read [Intermediate rotation](#intermediate-rotation) first.

### Roboform entries you will create

| Entry name | What it holds |
|---|---|
| `homelab-ca root key (encrypted)` | The encrypted `root_ca_key` blob, base64-or-armored as written below. |
| `homelab-ca root key passphrase` | Passphrase that decrypts the root key. |
| `homelab-ca intermediate key passphrase` | Passphrase step-ca uses to decrypt `intermediate_ca_key` at startup. |
| `homelab-ca JWK provisioner password` | Fleet-wide password for the `ansible-jwk` provisioner. |

### 1. Initialise the CA

```sh
mkdir -p ~/step-ca-bootstrap && cd ~/step-ca-bootstrap
export STEPPATH="$PWD/.step"
step ca init \
  --deployment-type=standalone \
  --name homelab-ca \
  --dns ca.home \
  --address :443 \
  --provisioner admin
```

`step ca init` will interactively prompt for:

- Root key password — generate a 32+ char random passphrase, save to
  Roboform as `homelab-ca root key passphrase`, then paste it here.
- Intermediate key password — generate a *separate* 32+ char passphrase,
  save as `homelab-ca intermediate key passphrase`, then paste here.
- Admin provisioner password — generate, save under a temporary
  Roboform note; this provisioner is purely for `step ca` admin
  operations on `wrkdev` and is not used by the cluster or fleet.

When the command returns, `$STEPPATH/secrets/` contains
`root_ca_key`, `intermediate_ca_key`, and the admin provisioner key,
each encrypted with its respective passphrase. `$STEPPATH/certs/`
contains `root_ca.crt` and `intermediate_ca.crt` (public, safe).

### 2. Move the root key into Roboform

```sh
cat .step/secrets/root_ca_key
```

Copy the entire armored block (`-----BEGIN ENCRYPTED PRIVATE KEY-----`
through `-----END ENCRYPTED PRIVATE KEY-----`) into Roboform under
`homelab-ca root key (encrypted)`.

Round-trip verify before deleting the on-disk copy:

```sh
# Paste the Roboform copy back to a scratch file
cat > /tmp/root_check.pem    # paste, Ctrl-D
diff -q /tmp/root_check.pem .step/secrets/root_ca_key && echo OK
shred -u /tmp/root_check.pem
```

Only after `OK`:

```sh
shred -u .step/secrets/root_ca_key
```

The encrypted intermediate key stays on disk for now — step 7 hands it
to the `step_ca` role.

### 3. Configure 47-day leaf claims

Edit `.step/config/ca.json`. Under the top-level `authority.claims`
key (create it if absent), set:

```json
"claims": {
  "defaultTLSCertDuration":  "1128h",
  "maxTLSCertDuration":      "1128h",
  "minTLSCertDuration":      "5m"
}
```

`1128h = 47 × 24h`. The same claim block applies fleet-wide unless a
provisioner overrides it; steps 4 and 5 do not override.

### 4. Add the ACME provisioner

```sh
step ca provisioner add acme --type ACME
```

This appends a JSON object to `authority.provisioners` in `ca.json`.
No additional configuration needed — the global claims from step 3
apply.

### 5. Add the JWK provisioner with SAN policy

```sh
step ca provisioner add ansible-jwk --type JWK --create
```

The `--create` flag generates a fresh JWK keypair and prompts for a
password to encrypt the private JWK. Generate a 32+ char random
passphrase and save to Roboform as
`homelab-ca JWK provisioner password`, then paste here.

Record the intended scope by editing the new provisioner entry in
`ca.json`. The allow list is **fully enumerated** — no wildcards. Every
name the CA should sign appears literally; adding a new managed host
means updating this list in the `step_ca` role's `ca.json` and running
its playbook (Conventions).

> **Not enforced.** step-ca 0.30.2 ignores per-provisioner name policy
> in a file-based `ca.json`: those fields are filled only through the
> remote-management API, which this estate does not use. The list
> below is dead configuration and the provisioner signs any name —
> OpenBao's listener leaf (`secrets.home`, `srvvault1.home`) is not on
> it. The control is wanted and not in place.

```json
{
  "type": "JWK",
  "name": "ansible-jwk",
  "key":  { ... },
  "encryptedKey": "...",
  "options": {
    "x509": {
      "allow": {
        "dns": [
          "pve",  "pve.home",
          "pve1", "pve1.home",
          "pve2", "pve2.home",
          "kubernetes-api",     "kubernetes-api.home",
          "kubernetes-api-dev", "kubernetes-api-dev.home",
          "kubernetes",
          "kubernetes.default",
          "kubernetes.default.svc",
          "kubernetes.default.svc.cluster.local"
        ],
        "ip": [
          "127.0.0.1",
          "172.17.0.1",
          "172.19.0.1"
        ]
      }
    }
  }
}
```

Notes:

- **PVE consumers** carry only DNS SANs (short + `.home` FQDN); they
  are reached by hostname only, so the leaf cert has no IP SAN.
- **K8s API server certs** (prd + dev) are served *additively* via the
  kube-apiserver's `--tls-sni-cert-key` SNI flag — they do **not**
  replace microk8s's own `server.crt`, so microk8s's internal PKI and
  every kubeconfig are left untouched. Each leaf therefore carries only
  the homelab-facing name it answers on:
  - prd: `kubernetes-api` + `kubernetes-api.home` — the HA VIP.
  - dev: `kubernetes-api-dev` + `kubernetes-api-dev.home` — the alias
    pointing at srvk8sdev.

  These are DNS-only — no IP SAN is needed, since SNI matches on the
  hostname the client dials.
- The remaining k8s entries in the `dns` / `ip` allow-lists above
  (`kubernetes`, `kubernetes.default*`, `127.0.0.1`, `172.17.0.1`,
  `172.19.0.1`) were provisioned for an earlier design that *replaced*
  microk8s's serving cert. That design was dropped — replacing
  `server.crt` with a homelab leaf breaks the control plane, since
  kubelet / controller-manager / kubeconfigs all validate against
  microk8s's own CA. The entries are harmless (an allow-list only
  permits, it never compels) and may be trimmed at a future ceremony
  touch; no IP SAN goes on the SNI leaf.
- Microk8s's stock self-signed cert keeps all its own SANs and CA —
  internal clients are unaffected by the additive SNI leaf.
- **When adding a new JWK consumer**: append its short + FQDN to the
  `dns` list in the `step_ca` role's `ca.json` and run its playbook
  alongside the role change that wires it in. Issuance does not fail without it while the list is not
  enforced (above); keeping it current keeps the intended scope
  written down for #993.

Validate the JSON before continuing:

```sh
jq . .step/config/ca.json > /dev/null && echo OK
```

### 6. Export the root cert into the Ansible repo

```sh
cp .step/certs/root_ca.crt \
  ~/source/Ansible/ansible/roles/baseline/files/homelab-root.crt
```

This file is public; commit it alongside the `baseline` role change.
Recipients trust the root by file content — keep the PEM-armored form
exactly as exported.

### 7. Hand the CA's material to the `step_ca` role

StepCaDeploy runs the upstream `step-certificates` chart in
`existingSecrets` mode (`config/prd/values.yaml`). The chart reads the
CA's material from five Secrets in `step-ca-prd`, which the Ansible
repo's `step_ca` role writes from its files, each value a file's exact
bytes (`roles/step_ca/vars/main.yml` maps them):

| Secret | Key | File in `ansible/roles/step_ca/files/` |
|---|---|---|
| `step-ca-certs` | `root_ca.crt` | none: `roles/baseline/files/homelab-root.crt` (step 6) |
| `step-ca-certs` | `intermediate_ca.crt` | `intermediate_ca.crt` |
| `step-ca-certs` | `ssh_host_ca_key.pub` | `ssh_host_ca_key.pub` |
| `step-ca-secrets` | `intermediate_ca_key` (encrypted PEM) | `intermediate_ca_key`, vaulted |
| `step-ca-secrets` | `ssh_host_ca_key` | `ssh_host_ca_key`, vaulted |
| `step-ca-ca-password` | `password` | `intermediate_ca_password`, vaulted |
| `step-ca-ssh-host-ca-password` | `password` | `ssh_host_ca_password`, vaulted |
| `step-ca-config` | `ca.json`, `defaults.json` | `ca.json`, `defaults.json`, vaulted |

The SSH host CA's three files belong to
[Enabling the SSH host CA](#enabling-the-ssh-host-ca), not to this
ceremony. Put this ceremony's material into the others:

```sh
cd ~/source/Ansible/ansible
s=~/step-ca-bootstrap/.step
cp "$s/certs/intermediate_ca.crt" roles/step_ca/files/intermediate_ca.crt
poetry run ansible-vault encrypt --output roles/step_ca/files/intermediate_ca_key \
  "$s/secrets/intermediate_ca_key"
read -rs pw    # paste `homelab-ca intermediate key passphrase` from Roboform, then Enter
printf '%s' "$pw" | poetry run ansible-vault encrypt --output roles/step_ca/files/intermediate_ca_password -
unset pw
```

Then edit `ca.json` and `defaults.json` with
`poetry run ansible-vault edit roles/step_ca/files/<file>`, bringing in,
from `$s/config/`, the `authority` block's provisioners and claims
(steps 1 and 3–5) and the new root's `fingerprint`. Keep the role's
paths, which are the pod's (`/home/step/certs/…`,
`/home/step/secrets/intermediate_ca_key`, `/home/step/db`,
`/home/step/config/ca.json`) and not the ceremony directory's, and keep
`ca.json`'s `ssh` block. Check that both still parse, then run the
playbook, which restarts step-ca on the new material:

```sh
poetry run ansible-vault view roles/step_ca/files/ca.json | jq empty
poetry run ansible-vault view roles/step_ca/files/defaults.json | jq empty
poetry run ansible-playbook playbooks/step-ca.yml
```

Check that the CA serves this ceremony's root:

```sh
curl -s --cacert "$s/certs/root_ca.crt" https://ca.home/roots.pem \
  | diff - "$s/certs/root_ca.crt"
```

`diff` prints nothing. step-ca then serves TLS under the new chain, so
the intermediate key decrypted, and the local `intermediate_ca_key`
file is no longer needed. Commit the role's files with
`homelab-root.crt` and push.

### 8. Encrypt the JWK provisioner password for Ansible

The `internal_tls` and `ssh_host_cert` roles read the JWK password from
ansible-vault:

```sh
cd ~/source/Ansible/ansible
read -rs pw    # paste `homelab-ca JWK provisioner password` from Roboform, then Enter
printf '%s' "$pw" | poetry run ansible-vault encrypt_string --stdin-name internal_tls_jwk_provisioner_password
unset pw
```

Paste the printed `!vault |` block into
`inventories/prd/group_vars/all/vips.yml`, replacing any
`internal_tls_jwk_provisioner_password` there. Commit the encrypted
blob.

### 9. Clean up the bootstrap directory

After steps 7 and 8 have succeeded and step 7's `diff` has shown
step-ca serving:

```sh
cd ~ && shred -u step-ca-bootstrap/.step/secrets/*
rm -rf step-ca-bootstrap
```

The CA's material now lives in the `step_ca` role's files, and its
database on StepCaDeploy's PVC `step-ca-db-pvc`. The operator's
recovery path if everything is lost: see
[Intermediate rotation](#intermediate-rotation) (the root in Roboform
is the recovery anchor).

---

## Enabling the SSH host CA

Extends the existing X.509 CA so it also signs SSH **host**
certificates. Every managed host then carries one, and Ansible
verifies them through a single committed `@cert-authority` line
instead of a pinned per-host key. Driven by
[`/work/AnsibleSpecs/slices/ssh-host-ca.md`](../../../AnsibleSpecs/slices/ssh-host-ca.md);
the `ssh_host_cert` Ansible role does all issuance and renewal
afterward.

One-shot. The X.509 root in Roboform is **not** involved — an SSH CA
is its own trust anchor, a plain ed25519 keypair independent of the
X.509 hierarchy.

### Roboform entries you will create

| Entry name | What it holds |
|---|---|
| `homelab-ca SSH host CA key (encrypted)` | The encrypted SSH host CA private key. |
| `homelab-ca SSH host CA key passphrase` | Passphrase decrypting it. |

### 1. Generate the SSH host CA keypair

From `wrkdev`, in a fresh directory:

```sh
d=$(mktemp -d) && cd "$d"
ssh-keygen -t ed25519 -f ssh_host_ca -C homelab-ssh-host-ca
```

Generate a 32+ char passphrase at the prompt, save it to Roboform as
`homelab-ca SSH host CA key passphrase`. This writes `ssh_host_ca`
(encrypted private key) and `ssh_host_ca.pub` (public key).

Copy the **private** key into Roboform under
`homelab-ca SSH host CA key (encrypted)`, with the same
round-trip-verify-before-shred discipline as the X.509 root (step 2
of the day-zero ceremony).

### 2. Hand the SSH host CA key to the `step_ca` role

Put the keypair and its passphrase into the role's three SSH host CA
files (day-zero step 7's table) and run the playbook:

```sh
cd ~/source/Ansible/ansible
cp "$d/ssh_host_ca.pub" roles/step_ca/files/ssh_host_ca_key.pub
poetry run ansible-vault encrypt --output roles/step_ca/files/ssh_host_ca_key "$d/ssh_host_ca"
read -rs pw    # paste `homelab-ca SSH host CA key passphrase` from Roboform, then Enter
printf '%s' "$pw" | poetry run ansible-vault encrypt --output roles/step_ca/files/ssh_host_ca_password -
unset pw
poetry run ansible-playbook playbooks/step-ca.yml
```

Then set `existingSecrets.sshHostCa: true` in StepCaDeploy's
`config/prd/values.yaml`, a chart value rather than material, and push.
Argo's sync rolls step-ca onto the `step-ca-ssh-host-ca-password` mount
and the `--ssh-host-password-file` flag. The playbook goes first: the
pod does not start while that Secret is missing.

### 3. Add the `ssh` block and host policy to `ca.json`

`ca.json` is the role's vaulted `roles/step_ca/files/ca.json`:

```sh
poetry run ansible-vault edit roles/step_ca/files/ca.json
```

Add a top-level `ssh` block pointing at the mounted host CA key
(confirm the path against the rendered pod):

```json
"ssh": {
  "hostKey": "/home/step/secrets/ssh_host_ca_key"
}
```

The `ansible-jwk` provisioner already carries `enableSSHCA: true`
and an empty `"options.ssh": {}` (no policy = allow-all on
principals). Leave the SSH policy empty — the slice's whole point is
that adding a VM needs no committed-file change, and ssh's own
principal-vs-connect-target check is what actually scopes a forged
cert: the `@cert-authority` line only lives in Ansible's
known_hosts, Ansible only connects to homelab hostnames, so a cert
with a non-homelab principal is unusable against this trust scope.

Add SSH host durations to the provisioner's `claims`:

```json
"claims": {
  "enableSSHCA": true,
  "disableRenewal": false,
  "allowRenewalAfterExpiry": false,
  "disableSmallstepExtensions": false,
  "minHostSSHCertDuration": "5m0s",
  "maxHostSSHCertDuration": "1128h0m0s",
  "defaultHostSSHCertDuration": "1128h0m0s"
}
```

`1128h = 47 days` — the same lifetime as the X.509 leaves; the
`ssh_host_cert` role re-signs under a 14-day threshold.

Check that the JSON still parses, then run the playbook. It restarts
step-ca, which reads `ca.json` at process start only:

```sh
poetry run ansible-vault view roles/step_ca/files/ca.json | jq empty
poetry run ansible-playbook playbooks/step-ca.yml
```

Check that the restart took: `curl -sk https://ca.home/provisioners`
lists the provisioner you edited.

### 4. Verify SSH issuance

Generate a throwaway keypair in `/tmp/` and sign it against the
redeployed CA. **Do not** pass `/etc/ssh/ssh_host_ed25519_key.pub`
here — `step ssh certificate --sign` writes the cert next to the
input key (`<key>-cert.pub`), so signing the canonical host key
overwrites whatever cert is at `/etc/ssh/ssh_host_ed25519_key-cert.pub`
with one carrying `test.home` as its principal, and sshd then serves
that cert until the role next re-issues (which it won't, until the
14-day threshold).

```sh
ssh-keygen -t ed25519 -f /tmp/sshca-test -N '' -q
step ssh certificate --host --sign --principal test.home test.home \
  /tmp/sshca-test.pub \
  --provisioner ansible-jwk \
  --provisioner-password-file <(printf '%s' '<JWK pw>') \
  --ca-url https://ca.home \
  --root ~/source/Ansible/ansible/roles/baseline/files/homelab-root.crt
ssh-keygen -L -f /tmp/sshca-test-cert.pub | grep -A1 'Principals\|Valid'
shred -u /tmp/sshca-test /tmp/sshca-test.pub /tmp/sshca-test-cert.pub
```

Expect a 47-day validity window and `test.home` as the sole principal.

### 5. Commit the SSH host CA public key to the Ansible repo

The committed `@cert-authority` line is what makes every managed host
verifiable. Put `ssh_host_ca.pub`'s contents into
`ansible/files/known_hosts.d/homelab`:

```
@cert-authority * <contents of ssh_host_ca.pub>
```

The `*` host pattern is intentional — scoping is enforced by the
certificate's own principals, not the known_hosts pattern. This file,
plus the `ansible.cfg` and per-host playbook changes, is the
ssh-host-ca "switch" commit; the `step_ca` role's files from steps 2
and 3 go in with it.

### 6. Clean up

`shred -u "$d"/*` and remove `$d`. The SSH host CA private key then
lives only in Roboform and, ansible-vault'd, in the `step_ca` role,
which writes it into the cluster Secret.

---

## Windows trust install

Run on `wrkdevwin` and any other Windows machine the operator uses to
hit homelab URLs.

1. Copy `homelab-root.crt` (from
   `~/source/Ansible/ansible/roles/baseline/files/homelab-root.crt` on
   `wrkdev`, or `kubectl -n step-ca-prd exec ... cat ...` if the file is
   otherwise unavailable) to the Windows machine.
2. Open an **elevated** PowerShell.
3. ```powershell
   certutil -addstore -f "ROOT" homelab-root.crt
   ```
4. Verify in `certmgr.msc` → Trusted Root Certification Authorities →
   Certificates that `homelab-ca` is present.

### Firefox

Firefox keeps its own trust store. Either:

- **Per-profile**: Settings → Privacy & Security → View Certificates
  → Authorities → Import → select `homelab-root.crt` → tick "Trust
  this CA to identify websites".
- **Enterprise-roots flag**: `about:config` → set
  `security.enterprise_roots.enabled` to `true`. Firefox then trusts
  whatever the Windows trust store trusts. Preferred for managed
  workstations.

### Per-machine smoke test

Hit `https://ca.home/health` in Chrome and Firefox. Both should show
a clean cert without warnings.

---

## Intermediate rotation

When to do this:

- Suspected intermediate compromise.
- Routine rotation (no automation; do it deliberately when the
  operator chooses, not on a calendar).
- After step-ca version upgrade if upstream advises re-issuing the
  intermediate.

A new intermediate does not retire the old one: the old certificate
chains to the same root until it expires, and no client here checks
revocation. Retiring a compromised intermediate takes a root rotation
([`step-ca-root-rotation.md`](step-ca-root-rotation.md)).

The root stays in Roboform throughout. Leaf re-issuance for every
consumer follows on the weekly `IaC/Scheduled Certs` run, as each leaf
enters its 14-day renewal window (so ≤47 days for the whole fleet).

### 1. Reconstitute the root on `wrkdev`

```sh
mkdir -p ~/step-ca-rotate && cd ~/step-ca-rotate
export STEPPATH="$PWD/.step"
step path  # confirms STEPPATH

mkdir -p .step/secrets .step/certs
# Paste encrypted root key from Roboform
cat > .step/secrets/root_ca_key  # paste, Ctrl-D
# Public root cert from the Ansible repo
cp ~/source/Ansible/ansible/roles/baseline/files/homelab-root.crt \
  .step/certs/root_ca.crt
```

### 2. Generate a fresh intermediate

```sh
step certificate create 'homelab-ca Intermediate CA' \
  .step/certs/intermediate_ca.crt \
  .step/secrets/intermediate_ca_key \
  --profile intermediate-ca \
  --ca .step/certs/root_ca.crt \
  --ca-key .step/secrets/root_ca_key \
  --not-after 87600h   # 10 years; matches step-ca default
```

You will be prompted for:

- Root key passphrase — paste from Roboform.
- New intermediate key passphrase — generate, save to Roboform as
  `homelab-ca intermediate key passphrase` (overwriting the old entry
  *after* step 4 succeeds).

### 3. Put the new intermediate into the `step_ca` role

```sh
cd ~/source/Ansible/ansible
r=~/step-ca-rotate/.step
cp "$r/certs/intermediate_ca.crt" roles/step_ca/files/intermediate_ca.crt
poetry run ansible-vault encrypt --output roles/step_ca/files/intermediate_ca_key \
  "$r/secrets/intermediate_ca_key"
read -rs pw    # paste the new intermediate key passphrase, then Enter
printf '%s' "$pw" | poetry run ansible-vault encrypt --output roles/step_ca/files/intermediate_ca_password -
unset pw
poetry run ansible-playbook playbooks/step-ca.yml
```

The run changes `step-ca-certs`, `step-ca-secrets` and
`step-ca-ca-password` and restarts step-ca on them. If step 4 fails,
`git restore roles/step_ca/files` and run the playbook again: step-ca
restarts on the old intermediate. Commit and push the three files once
step 4 has verified.

### 4. Verify

```sh
root_crt=~/source/Ansible/ansible/roles/baseline/files/homelab-root.crt

curl --cacert "$root_crt" https://ca.home/health
# {"status":"ok"}

step ca roots --ca-url https://ca.home --root "$root_crt"
```

The intermediate's serial should match the freshly-generated one.

### 5. Clean up + update Roboform

```sh
cd ~ && shred -u step-ca-rotate/.step/secrets/*
rm -rf step-ca-rotate
```

Update the Roboform entry `homelab-ca intermediate key passphrase` to
the new passphrase. **Only after** step 4 verified.

Leaf certs in the field keep working with the old chain until they
renew; renewal under the new intermediate happens on the weekly
`IaC/Scheduled Certs` run as each leaf reaches its 14-day threshold. To
force-renew everything early, `rm <cert.pem>` on each consumer (or
temporarily bump `internal_tls_renewal_threshold_days` — see the role's
README) and run `poetry run ansible-playbook playbooks/renew-internal-tls.yml`,
which reaches every leaf in the fleet.

---

## JWK provisioner password rotation

When to do this:

- Suspected leak of the password (ansible-vault file mishandled,
  laptop compromise, etc.).
- Routine rotation.

The password encrypts `ansible-jwk`'s private key, which `ca.json`
holds as the provisioner's `encryptedKey`. That ciphertext is public:
step-ca serves it at `https://ca.home/provisioners`, and StepCaDeploy's
history holds it. A key only re-encrypted under a new password still
signs for whoever holds the old one, so the rotation replaces the key
pair. `ca.json` has no `authority.enableAdmin`, so step-ca offers no
remote provisioner API: the key is replaced in the `step_ca` role's
`ca.json` and reaches step-ca through the role's playbook. The fleet's
copy of the password is `internal_tls_jwk_provisioner_password` in
`ansible/inventories/prd/group_vars/all/vips.yml`, which the
`internal_tls` and `ssh_host_cert` roles read. The two change together:
once step-ca runs on the new `ca.json`, only the new password signs.

### 1. Generate a new password

```sh
openssl rand -base64 32
```

Save to Roboform under a temporary name like
`homelab-ca JWK provisioner password (new)`.

### 2. Replace the key in the role's `ca.json`

```sh
cd ~/source/Ansible/ansible
t=$(mktemp -d)
poetry run ansible-vault decrypt --output "$t/ca.json" roles/step_ca/files/ca.json
step crypto jwk create "$t/pub.json" "$t/priv.json"   # prompts for the new password
step crypto jose format < "$t/priv.json" > "$t/priv.compact"
jq --slurpfile pub "$t/pub.json" --rawfile key "$t/priv.compact" \
  '(.authority.provisioners[] | select(.name == "ansible-jwk"))
     |= (.key = $pub[0] | .encryptedKey = ($key | rtrimstr("\n")))' \
  "$t/ca.json" > "$t/ca.new.json"
poetry run ansible-vault encrypt --output roles/step_ca/files/ca.json "$t/ca.new.json"
shred -u "$t"/* && rmdir "$t"
```

`jwk create` makes an EC P-256 key, the kind `provisioner add --create`
makes (day-zero step 5), and writes its private half as a JWE in JSON
serialization; `jose format` turns it into the compact form `ca.json`
holds. The new key has a new `kid`. The `internal_tls` and
`ssh_host_cert` roles look the provisioner up by name, so nothing pins
the old one.

### 3. Re-encrypt the ansible-vault entry

```sh
read -rs pw    # paste the new password, then Enter
printf '%s' "$pw" | poetry run ansible-vault encrypt_string --stdin-name internal_tls_jwk_provisioner_password
unset pw
```

Replace the `internal_tls_jwk_provisioner_password: !vault |` block in
`inventories/prd/group_vars/all/vips.yml` with the printed one.

### 4. Apply, then verify on one VM

```sh
poetry run ansible-playbook playbooks/step-ca.yml
```

The run restarts step-ca on the new `ca.json`. From here the old
password signs nothing, so a certificate run from `main`, such as the
weekly `IaC/Scheduled Certs`, fails until the push below.

Pick a low-blast-radius host (a scratch VM or one PVE node). Force a
re-issue from this checkout, which carries the new password:

```sh
# On the target host
rm /etc/pve/local/pveproxy-ssl.pem    # or whichever cert

# From ~/source/Ansible/ansible — a missing leaf is re-issued regardless
# of the renewal threshold. IaC/Scheduled Drift cannot do this: it is
# --check-only, so it reports the missing leaf and signs nothing.
poetry run ansible-playbook playbooks/renew-internal-tls.yml --limit pve
```

Watch the role re-issue under the new password and the consumer
reload cleanly. Then commit `roles/step_ca/files/ca.json` and
`inventories/prd/group_vars/all/vips.yml` together and push, and
update Roboform: delete the old JWK entry, rename `(new)` →
`homelab-ca JWK provisioner password`.

If the re-issue fails,
`git restore roles/step_ca/files/ca.json inventories/prd/group_vars/all/vips.yml`
and run `playbooks/step-ca.yml` again: step-ca restarts on the old key.

---

## Monitoring smoke test

Run after the §J commits (cert-expiry exporter + alert rule) land.

### VM consumers

1. Pick a managed VM with a step-ca-issued cert (e.g. `pve`).
2. SSH in, check the node-exporter textfile:
   ```sh
   cat /var/lib/node_exporter/textfile_collector/cert_expiry_*.prom
   ```
   Expect one `cert_expiry_seconds{...}` line per consumer cert, with
   a value roughly equal to `47 × 86400` immediately after issue.
3. From the workstation, confirm Prometheus is scraping the metric:
   ```sh
   curl -s 'http://prometheus.home/api/v1/query?query=cert_expiry_seconds' | jq
   ```

### In-cluster consumers

```sh
kubectl get certificate -A
kubectl describe certificate <name> -n <ns>
```

`Ready=True`, `Not After` ≈ 47 days from now.

cert-manager exposes the equivalent metric via its built-in exporter:

```sh
curl -s 'http://prometheus.home/api/v1/query?query=certmanager_certificate_expiration_timestamp_seconds' | jq
```

### Alert plumbing

Temporarily shorten one consumer's `internal_tls_renewal_threshold_days`
to a value > 30 (e.g. 35). On the next renewal run the
cert is still well above expiry but below the alert window of 17
days — actually, the alert fires on remaining time below 17d, not on
threshold-vs-validity. To force a real alert without waiting weeks,
re-issue a leaf with a very short validity:

```sh
# From wrkdev, with step bootstrapped against ca.home
step ca certificate test.home /tmp/test.pem /tmp/test.key \
  --provisioner ansible-jwk \
  --provisioner-password-file <(printf '%s' '<JWK pw>') \
  --not-after 16h
```

Drop `/tmp/test.pem` into the textfile collector path on a scratch
host (or wire it through the same exporter the role uses) and confirm
the alert fires in Prometheus / Alertmanager within one scrape
interval. Remove the test cert afterwards.
