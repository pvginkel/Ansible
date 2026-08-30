# Runbook: expired SSH host certificate (host is UNREACHABLE to Ansible)

## Symptom

Every Ansible play against the host dies before its first task:

```
fatal: [srvk8s1]: UNREACHABLE! => {"msg": "Task failed: Data could not be sent to
remote host \"srvk8s1\". Make sure this host can be reached over ssh:
Certificate invalid: expired\r\nHost key verification failed.", "unreachable": true}
```

The host itself is fine — it answers SSH, the workloads on it are running. Only
host-key *verification* fails.

## Cause

Every managed host serves a step-ca SSH host certificate (the `ssh_host_cert`
role); clients trust them through the single `@cert-authority` line in
`ansible/files/known_hosts.d/homelab`. The certificates last **47 days** and
`ssh_host_cert` re-signs inside the last **14**, so an apply has to reach each
host at least every 33 days. When none does, the certificate lapses and ssh
refuses the host.

`IaC/Scheduled Certs` (weekly, `Jenkinsfile.iac-scheduled-certs`) exists to make
that cadence guaranteed — for these host certificates and, in stages of its own,
for the `internal_tls` X.509 leaves. A lapse means that job has been failing or
unscheduled — **check it before doing anything else**, or you will be back here
in 47 days.

## Confirm it (read-only)

Certificates are world-readable, so this needs no sudo. From a host that still
verifies, or with `-o HostKeyAlgorithms=ssh-ed25519` to bypass the bad cert:

```sh
ssh -o HostKeyAlgorithms=ssh-ed25519 ansible@srvk8s1 \
    'ssh-keygen -L -f /etc/ssh/ssh_host_ed25519_key-cert.pub | grep Valid:'
```

To sweep the fleet, loop that over the hosts in `inventories/prd/hosts.yml`.
Anything inside 14 days is due; anything past its `to` date is already lapsed.

## Fix

`playbooks/reissue-host-cert.yml` re-signs over a bootstrap channel that does not
depend on the broken certificate. It pins the target's bare host key from
`terraform output host_pubkeys` — authoritative, no TOFU — and connects with
`HostKeyAlgorithms=ssh-ed25519` alone so sshd serves that bare key instead of the
expired certificate. Several hosts at once, comma-separated:

```sh
cd ansible && poetry run ansible-playbook playbooks/reissue-host-cert.yml \
    -e reissue_target=srvk8s1,srvk8s2,srviac
```

Requires the `step` CLI on the controller (wrkdev or the iac container) and the
fleet vault passphrase. It only covers VMs Terraform builds from scratch
(`from_scratch = true` in `terraform/prd/vms.tf`) — those are the ones whose host
key Terraform pins.

Then confirm normal verification is restored, through the committed
`@cert-authority` line with no special flags:

```sh
cd ansible && poetry run ansible srvk8s1,srvk8s2,srviac -m ping
```

## Notes

- **Do not** reach for `HostKeyAlgorithms=ssh-ed25519-cert-v01@openssh.com,ssh-ed25519`
  as a workaround. sshd serves the first algorithm the client offers that it can
  satisfy, and an expired-cert host still has one to serve — so listing the cert
  type first hands you back the very certificate you are trying to replace, and
  there is no client-side fallback to the bare key. Offer `ssh-ed25519` alone.
- A certificate that is *missing* rather than expired (cloud-init regenerating
  host keys on a new instance-id) has the same symptom and the same fix.
- For a brand-new VM, use `rebuild-k8s.yml` — full bootstrap — not this.
- Renewal design and the role's inputs: `ansible/roles/ssh_host_cert/README.md`.
  CA-side provisioner setup: [`step-ca-bootstrap.md`](step-ca-bootstrap.md).
