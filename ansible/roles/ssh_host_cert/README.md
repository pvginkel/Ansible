# `ssh_host_cert` role

Issues and renews a homelab `step-ca` **SSH host certificate** for a
managed host's ed25519 host key, and serves it from `sshd`. Once every
host carries a certificate, host-key verification collapses from a
per-host pinned key in `files/known_hosts.d/` to a single committed
`@cert-authority *` line.

Design: `/work/AnsibleSpecs/slices/ssh-host-ca.md`.

## How it is used

`ssh_host_cert` is a **top-level role**, not an `include_role` consumer
like `internal_tls`. Every managed host gets exactly one host
certificate for its own hostname, so it composes via play ordering —
listed **last** in `site.yml`, `site-k8s.yml` and `rebuild-k8s.yml`,
after `baseline` (and, on k8s nodes, after `microk8s`).

It needs no per-inclusion vars: principals and paths are derived from
`inventory_hostname` and role defaults.

## Behaviour

1. **Install** — adds Smallstep's apt repository and installs
   `step-cli` (a verbatim parallel of `internal_tls`'s install step;
   each role owns its dependency).
2. **Check** — `ssh-keygen -L` against the existing certificate; the
   role re-signs when the certificate is missing or within
   `ssh_host_cert_renewal_threshold_days` of expiry, and is otherwise
   a no-op.
3. **Issue (split)** — mirrors `internal_tls`:
   - On the **controller**: write the JWK password to a `/dev/shm`
     tempfile, `step ca token --ssh --host` to mint a short-lived
     token scoped to this host's principals, delete the tempfile in an
     `always` block. The provisioner password never reaches the target.
   - On the **target**: `step ssh certificate --host --sign --token …`
     signs the existing `ssh_host_ed25519_key.pub` into
     `ssh_host_ed25519_key-cert.pub`. The host private key never moves.
4. **Activate** — write a `sshd_config.d` drop-in with `HostKey` +
   `HostCertificate`, validate the merged config with `sshd -t`, and
   notify a `reload` (not restart — the running SSH connection
   survives).

Cadence comes from whatever runs the playbook — `iac-scheduled-drift`
for the steady state. The renewal threshold makes the role idempotent
under that cadence.

## Inputs

All optional — see `defaults/main.yml`.

| Variable | Default | Meaning |
|---|---|---|
| `ssh_host_cert_host_key` | `/etc/ssh/ssh_host_ed25519_key` | Host key signed; cert installed at `<key>-cert.pub`. |
| `ssh_host_cert_principals` | `[hostname, hostname.home]` | Names a client may verify the host as. |
| `ssh_host_cert_renewal_threshold_days` | `14` | Re-sign when fewer than this many days remain. |
| `ssh_host_cert_ca_url` | `https://ca.home` | Homelab CA endpoint. |
| `ssh_host_cert_provisioner` | `ansible-jwk` | step-ca provisioner (SSH host policy required). |

`ssh_host_cert_jwk_provisioner_password` defaults to the fleet-wide
vaulted `internal_tls_jwk_provisioner_password` (`group_vars/all/vips.yml`)
— one provisioner issues both the X.509 leaves and the SSH host certs.
The role asserts it resolves.

## Requirements

- **Controller** (the iac agent container / `wrkdev`): the `step` CLI
  on `PATH`. The token mint anchors CA trust against the repo's copy
  of the homelab root (`roles/baseline/files/homelab-root.crt`).
- **Target**: `baseline` must have run — the role asserts the homelab
  root cert is in the trust store so the target trusts `ca.home`.
- The `ansible-jwk` provisioner must have an **SSH host policy** whose
  principals cover this host's short + FQDN names. The policy is
  pattern-based (`srv*`, `wrk*`, `pve*`, `*.home`) so adding a
  conventionally-named host needs no CA change. See
  `docs/runbooks/step-ca-bootstrap.md`.

## Notes

- The drop-in re-states `HostKey` for the ed25519 key only. Hosts
  therefore serve their ed25519 host key + certificate; the
  non-deterministic rsa/ecdsa keys `sshd` auto-generates are no longer
  offered — consistent with `ansible.cfg`'s long-standing
  `HostKeyAlgorithms=ssh-ed25519` pin.
- No cert-expiry metric is published (unlike `internal_tls`).
  Observability is deferred — a failed renewal fails the drift run
  loudly, which is the signal that matters.
