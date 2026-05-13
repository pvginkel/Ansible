# step-ca bootstrap and lifecycle runbook

Authoritative procedure for everything to do with the homelab CA that
cannot or should not be automated:

- [Day-zero ceremony](#day-zero-ceremony) — generate root + intermediate,
  configure ACME and JWK provisioners, export the root cert, hand the
  encrypted intermediate to the chart.
- [Windows trust install](#windows-trust-install) — one-shot per Windows
  machine the operator uses.
- [Intermediate rotation](#intermediate-rotation) — when the intermediate
  is compromised or routinely rotated.
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
  Ansible or HelmCharts checkouts. Nothing the ceremony produces should
  end up in git except the public root cert.

---

## Day-zero ceremony

One-shot. Re-running any step short of "rotate intermediate" is a
deviation — read [Intermediate rotation](#intermediate-rotation) first.

### Roboform entries you will create

| Entry name | What it holds |
|---|---|
| `homelab-ca root key (encrypted)` | The encrypted `root_ca_key` blob, base64-or-armored as written below. |
| `homelab-ca root key passphrase` | Passphrase that decrypts the root key. |
| `homelab-ca intermediate key passphrase` | Passphrase the chart will use to decrypt `intermediate_ca_key` at startup. |
| `homelab-ca JWK provisioner password` | Fleet-wide password for the `ansible-jwk` provisioner. |

### 1. Initialise the CA

```sh
mkdir -p ~/step-ca-bootstrap && cd ~/step-ca-bootstrap
export STEPPATH="$PWD/.step"
step ca init \
  --deployment-type=standalone \
  --name homelab-ca \
  --dns ca.home \
  --address :8443 \
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
to the chart.

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

Restrict issuance by editing the new provisioner entry in `ca.json`.
The allow list is **fully enumerated** — no wildcards. Every name the
CA may sign appears literally; adding a new managed host means
updating this list and restarting step-ca.

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
          "srvk8s1", "srvk8s1.home",
          "srvk8s2", "srvk8s2.home",
          "srvk8s3", "srvk8s3.home",
          "kubernetes",
          "kubernetes.default",
          "kubernetes.default.svc",
          "kubernetes.default.svc.cluster.local"
        ],
        "ip": [
          "<srvk8s1 vmbr0 IP>",
          "<srvk8s2 vmbr0 IP>",
          "<srvk8s3 vmbr0 IP>",
          "10.152.183.1"
        ]
      }
    }
  }
}
```

Notes:

- Each managed VM consumer gets two DNS entries: the short hostname
  and the `.home` FQDN, both of which appear on the leaf cert. Every
  internal endpoint should be reachable by either form, so both are
  signed.
- **IP SANs are only used on the k8s API server cert**, and only
  because microk8s's default kubeconfig points at the node IP
  (`https://<vmbr0-ip>:16443`); kubectl rejects the cert without a
  matching IP SAN. PVE and any future VM consumer that's reached
  purely by hostname must **not** carry an IP SAN, and the policy
  enforces that by only listing the three k8s node IPs + the cluster
  service IP. Look up the current IPs with:
  ```sh
  for h in srvk8s1 srvk8s2 srvk8s3; do
    echo -n "$h: "; ssh "$h.home" ip -4 -o addr show dev ens18 | awk '{print $4}' | cut -d/ -f1
  done
  ```
  Substitute the three placeholders in the JSON above. (If `ens18`
  isn't the primary interface on a node, adjust — the role uses
  `ansible_default_ipv4.address`.)
- `10.152.183.1` is the microk8s cluster service IP — kube-apiserver
  default for the in-cluster `kubernetes` Service. It's not a node IP
  but is reachable inside pods that hit `kubernetes.default.svc`.
- The four `kubernetes.*` DNS names are the in-cluster
  `kubernetes.default.svc.*` chain that pods use to reach the API.
- **When adding a new JWK consumer**: append its short + FQDN here,
  reload step-ca, *then* land the role change that wires it in. The
  role will fail with "not authorized" if the policy hasn't been
  updated first. Only add an IP entry if the consumer is forced to
  carry an IP SAN (microk8s is the only such case in v1).

Validate the JSON before continuing:

```sh
jq . .step/config/ca.json > /dev/null && echo OK
```

### 6. Export the root cert into the Ansible repo

```sh
cp .step/certs/root_ca.crt /work/Ansible/ansible/files/homelab-root.crt
```

This file is public; commit it alongside the `baseline` role change.
Recipients trust the root by file content — keep the PEM-armored form
exactly as exported.

### 7. Hand the encrypted intermediate to the chart

The HelmCharts `step-ca` deployment expects two pieces in a regular
k8s Secret named `step-ca-intermediate` in the chart's namespace:

| Secret key | Source |
|---|---|
| `intermediate_ca.key` | `.step/secrets/intermediate_ca_key` (encrypted PEM) |
| `password`            | `homelab-ca intermediate key passphrase` from Roboform |

Create the Secret **before** the chart is first installed, in the
target cluster (`dev` first, then `prd`):

```sh
kubectl -n step-ca create secret generic step-ca-intermediate \
  --from-file=intermediate_ca.key=.step/secrets/intermediate_ca_key \
  --from-literal=password='<paste intermediate passphrase>'
```

After the chart deploys and the pod confirms the intermediate
decrypts (look for `Serving HTTPS on :8443` in the pod logs), the
local `intermediate_ca_key` file is no longer needed — Roboform holds
the passphrase, the cluster holds the encrypted key.

### 8. Encrypt the JWK provisioner password for Ansible

The `internal_tls` role reads the JWK password from ansible-vault:

```sh
poetry run ansible-vault encrypt_string \
  --vault-id homelab@~/.ansible-vault-pass \
  --stdin-name internal_tls_jwk_provisioner_password \
  < <(printf '%s' '<paste JWK password from Roboform>')
```

Paste the resulting `!vault |` block into
`ansible/group_vars/all/vault.yml` (or wherever the existing vaulted
fleet-wide secrets live; check `ansible/group_vars/` layout). Commit
the encrypted blob.

### 9. Clean up the bootstrap directory

After steps 7 and 8 have succeeded and you have verified the chart pod
is serving:

```sh
cd ~ && shred -u step-ca-bootstrap/.step/secrets/*
rm -rf step-ca-bootstrap
```

The persistent state of the CA now lives in the chart's PVC. The
operator's recovery path if everything is lost: see
[Intermediate rotation](#intermediate-rotation) (the root in Roboform
is the recovery anchor).

---

## Windows trust install

Run on `wrkdevwin` and any other Windows machine the operator uses to
hit homelab URLs.

1. Copy `homelab-root.crt` (from
   `/work/Ansible/ansible/files/homelab-root.crt` on `wrkdev`, or
   `kubectl -n step-ca exec ... cat ...` if the file is otherwise
   unavailable) to the Windows machine.
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

The root stays in Roboform throughout. Leaf re-issuance for every
consumer happens organically on the next renewal cycle (≤47 days).

### 1. Reconstitute the root on `wrkdev`

```sh
mkdir -p ~/step-ca-rotate && cd ~/step-ca-rotate
export STEPPATH="$PWD/.step"
step path  # confirms STEPPATH

mkdir -p .step/secrets .step/certs
# Paste encrypted root key from Roboform
cat > .step/secrets/root_ca_key  # paste, Ctrl-D
# Public root cert from the Ansible repo
cp /work/Ansible/ansible/files/homelab-root.crt .step/certs/root_ca.crt
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

### 3. Replace the chart's Secret

```sh
kubectl -n step-ca create secret generic step-ca-intermediate \
  --from-file=intermediate_ca.key=.step/secrets/intermediate_ca_key \
  --from-literal=password='<new passphrase>' \
  --dry-run=client -o yaml | kubectl apply -f -
```

Restart the step-ca deployment to pick up the new intermediate:

```sh
kubectl -n step-ca rollout restart deployment step-ca
kubectl -n step-ca rollout status  deployment step-ca
```

### 4. Verify

```sh
curl --cacert /work/Ansible/ansible/files/homelab-root.crt \
  https://ca.home/health
# {"status":"ok"}

step ca roots --ca-url https://ca.home --root \
  /work/Ansible/ansible/files/homelab-root.crt
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
renew; renewal under the new intermediate is automatic at the 14-day
threshold. To force-renew everything early, see the
`internal_tls_renewal_threshold_days` knob in the role's README and
either bump it temporarily or `rm <cert.pem>` on each consumer.

---

## JWK provisioner password rotation

When to do this:

- Suspected leak of the password (ansible-vault file mishandled,
  laptop compromise, etc.).
- Routine rotation.

### 1. Generate a new password

```sh
openssl rand -base64 32
```

Save to Roboform under a temporary name like
`homelab-ca JWK provisioner password (new)`.

### 2. Re-encrypt the JWK provisioner on the CA

```sh
mkdir -p ~/step-jwk-rotate && cd ~/step-jwk-rotate
export STEPPATH="$PWD/.step"
step ca bootstrap --ca-url https://ca.home \
  --fingerprint $(step certificate fingerprint \
    /work/Ansible/ansible/files/homelab-root.crt)

step ca provisioner update ansible-jwk --password-file <(printf '%s' '<new password>')
```

`step ca provisioner update --password-file` re-encrypts the stored
JWK private key with the new password without re-creating the
provisioner (so the public key fingerprint, and thus issued-cert
provenance, is unchanged).

If your step-ca version doesn't support in-place password update,
fall back to `provisioner remove` + `provisioner add --create`. That
*does* change the JWK fingerprint; any pinned references must be
updated.

### 3. Re-encrypt the ansible-vault entry

```sh
cd /work/Ansible
poetry run ansible-vault encrypt_string \
  --vault-id homelab@~/.ansible-vault-pass \
  --stdin-name internal_tls_jwk_provisioner_password \
  < <(printf '%s' '<new password>')
```

Replace the existing `internal_tls_jwk_provisioner_password` block in
the vault file with the new one. Commit.

### 4. Verify on one VM

Pick a low-blast-radius host (a scratch VM or one PVE node). Force a
re-issue:

```sh
# On the target host
rm /etc/pve/local/pveproxy-ssl.pem    # or whichever cert
# Run the iac-scheduled-drift cycle, or invoke the consumer playbook
```

Watch the role re-issue under the new password and the consumer
reload cleanly. Then update Roboform: delete the old JWK entry,
rename `(new)` → `homelab-ca JWK provisioner password`.

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
to a value > 30 (e.g. 35). On the next iac-scheduled-drift cycle the
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
