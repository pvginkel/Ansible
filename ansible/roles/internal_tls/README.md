# `internal_tls` role

Issues and renews homelab `step-ca` leaf certificates for VM consumers,
using the **JWK provisioner**. It is a reusable role with no host-class
of its own — consumer roles pull it in via `include_role` with
per-inclusion vars.

Design: `/work/AnsibleSpecs/slices/internal-tls-step-ca.md` §D.

## How it is used

A consumer role includes it and defines its own reload handler:

```yaml
# roles/<consumer>/tasks/main.yml
- name: Issue the consumer's TLS leaf
  ansible.builtin.include_role:
    name: internal_tls
  vars:
    internal_tls_san_list:
      - myservice
      - myservice.home
    internal_tls_cert_path: /etc/myservice/tls.crt
    internal_tls_key_path: /etc/myservice/tls.key
    internal_tls_owner: myservice
    internal_tls_group: myservice
    internal_tls_mode: "0640"
    internal_tls_reload_handler: Reload myservice

# roles/<consumer>/handlers/main.yml
- name: Reload myservice
  ansible.builtin.systemd:
    name: myservice
    state: reloaded
```

## Inputs

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `internal_tls_san_list` | yes | — | SANs for the leaf. First entry becomes the CN. |
| `internal_tls_cert_path` | yes | — | Install path for the leaf certificate. |
| `internal_tls_key_path` | yes | — | Install path for the leaf private key. |
| `internal_tls_reload_handler` | yes | — | Handler name to notify when the cert changes. The caller defines it. |
| `internal_tls_owner` | no | `root` | Owner of the installed cert + key. |
| `internal_tls_group` | no | `root` | Group of the installed cert + key. |
| `internal_tls_mode` | no | `0640` | Mode of the installed cert + key. |
| `internal_tls_renewal_threshold_days` | no | `14` | Re-issue when the leaf has fewer than this many days left. |
| `internal_tls_ca_url` | no | `https://ca.home` | Homelab CA endpoint. |
| `internal_tls_provisioner` | no | `ansible-jwk` | step-ca provisioner used for issuance. |
| `internal_tls_textfile_dir` | no | `/var/lib/prometheus/node-exporter` | node-exporter textfile-collector directory the cert-expiry metric is written to. |

`internal_tls_jwk_provisioner_password` — the fleet-wide JWK password —
is a vaulted var in `group_vars/all/vips.yml`, not a role input. The
role asserts it is defined.

## Behaviour

1. **Install** — adds Smallstep's apt repository (signing key +
   `deb822` source) and installs `step-cli`.
2. **Check** — the role issues when *any* of these is true: the leaf
   is missing; `step certificate needs-renewal` reports it inside the
   renewal threshold; or its DNS SANs differ from
   `internal_tls_san_list` (read back via `step certificate inspect`).
   Otherwise it is a no-op.
3. **Issue (split)**:
   - On the **controller** (the iac agent container): write the JWK
     password to a `/dev/shm` tempfile, `step ca token` to mint a
     short-lived, SAN-scoped token, delete the tempfile in an `always`
     block. The provisioner password never reaches the target.
   - On the **target**: `step ca certificate --token …` — the keypair
     is generated locally and never leaves the host.
   - Apply ownership/mode and notify the caller's reload handler.
4. **Publish metric** — write `internal_tls_cert_not_after_seconds`
   (the leaf's absolute not-after epoch) to the node-exporter textfile
   collector at `internal_tls_textfile_dir`, one `.prom` file per cert.
   Written every run, so the metric exists for pre-existing certs and
   tracks whatever leaf is on disk. Prometheus alerts off it when a
   leaf nears expiry — see the slice's §J. **Skipped** when
   `internal_tls_textfile_dir` is absent — a host with no
   prometheus-node-exporter package, e.g. a k8s node running
   node_exporter as an in-cluster DaemonSet.

Cadence comes from whatever calls the consumer role (iac-scheduled-drift
for the steady state). The threshold gate makes the role naturally
idempotent under that cadence.

## Requirements

- **Controller** (the iac agent container): the `step` CLI must be on
  `PATH`. The token mint anchors its TLS to the CA against the repo's
  copy of the homelab root (`roles/baseline/files/homelab-root.crt`),
  so the container does **not** need the homelab CA in its trust store.
- **Target**: the `baseline` role must have run — internal_tls relies
  on `/usr/local/share/ca-certificates/homelab-root.crt` being present
  so the target trusts `ca.home`. The role asserts this and fails with
  a clear message if baseline has not run.
- The JWK provisioner's SAN allow-policy must already permit every SAN
  in `internal_tls_san_list`; otherwise step-ca rejects issuance. See
  `docs/runbooks/step-ca-bootstrap.md`.

## Out of scope

- **In-cluster consumers** use cert-manager + step-ca's ACME
  provisioner, not this role.
