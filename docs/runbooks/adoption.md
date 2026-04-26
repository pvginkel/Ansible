# Adopting a host into Ansible management

How to bring a host under Ansible management when it was *not* provisioned via the Terraform + cloud-init path. Use this for the PVE physical nodes (`pve`, `pve1`, `pve2`), the operator workstation (`wrkdev`), and any future hand-installed Linux box.

For Terraform-provisioned VMs the host key arrives in the repo at `terraform apply` time and `bootstrap` runs as part of the normal `site.yml` flow — adoption is not needed.

## What `adopt.yml` does

Two plays:

1. On `localhost`: `ssh-keyscan` each target's existing ed25519 host key and write it into `ansible/files/known_hosts.d/<basename>` in the same `name,name.home keytype keydata` format Terraform uses.
2. On each target: apply the `bootstrap` role — creates the `ansible` automation user (UID 900, passwordless sudo) and the `pvginkel` operator user, drops their SSH keys.

After both plays succeed, append the new known_hosts file path to `ansible.cfg`'s `UserKnownHostsFile` and commit. From that point on the host is reachable via `site.yml`.

## Prerequisites

- The target's ed25519 host key is reachable over the network (a modern Debian/Ubuntu serves one by default; verify with `ssh-keyscan -t ed25519 <host>`).
- The operator can SSH to the target with elevated privileges. Two patterns:
  - **Linux host with operator account**: log in as `pvginkel` (or another sudoer), elevate via `sudo` — invoke `adopt.yml` with `-u pvginkel -K`.
  - **PVE node (root-only)**: log in as `root` directly using the `pve-root` key in `ssh-agent` — invoke with `-u root` (no `-K`; root needs no sudo password).
- The host resolves under `.home` from the workstation. The known_hosts entry that gets written includes both the short name and the FQDN.

## Run

From the `ansible/` directory:

```sh
poetry run ansible-playbook playbooks/adopt.yml \
  -i inventories/<inv> \
  -u <login user> [-K] \
  -e adoption_targets=<host_or_group_pattern> \
  -e adoption_known_hosts_file=<basename>
```

The two `-e` extras are required.

| Variable | What it is |
|---|---|
| `adoption_targets` | What Play 2's `hosts:` resolves to. A group name (`proxmox`), a single host (`wrkdev`), or a comma-separated list. Anything `ansible-inventory` would accept. |
| `adoption_known_hosts_file` | Basename written under `files/known_hosts.d/`. Pick something stable per inventory or per group — once added to `ansible.cfg`, the file is referenced by name forever. |

### Example: adopt `wrkdev` (operator workstation, dev inventory)

```sh
poetry run ansible-playbook playbooks/adopt.yml \
  -i inventories/dev \
  -u pvginkel -K \
  -e adoption_targets=wrkdev \
  -e adoption_known_hosts_file=dev
```

### Example: adopt the three PVE nodes (prd inventory)

```sh
poetry run ansible-playbook playbooks/adopt.yml \
  -i inventories/prd \
  -u root \
  -e adoption_targets=proxmox \
  -e adoption_known_hosts_file=proxmox
```

The PVE adoption order is operator's choice; the cluster keeps working through it. Default suggestion: pick a non-master node first (`pve2`), confirm `site.yml --check --diff` against it, then the second non-master (`pve1`), then the master (`pve`).

## Wire the new file into ansible.cfg

After `adopt.yml` succeeds, edit `ansible/ansible.cfg`'s `ssh_args` and add the new file to `UserKnownHostsFile`. The option takes a space-separated list:

```
-o "UserKnownHostsFile=files/known_hosts.d/scratch files/known_hosts.d/<basename>"
```

Commit `ansible.cfg` and the new `files/known_hosts.d/<basename>` together. From that commit onward, `site.yml` against the host works without the override that `adopt.yml` itself uses.

## Verify

```sh
poetry run ansible-playbook playbooks/site.yml -i inventories/<inv> --limit <host> --check --diff
```

A second run should report `changed=0`. If `--check` flags drift, that's expected on first contact — apply for real:

```sh
poetry run ansible-playbook playbooks/site.yml -i inventories/<inv> --limit <host>
```

## One-time post-adoption step

The `bootstrap` role does not set a password for `pvginkel`, so interactive `sudo` does not work yet. Set one (per `ansible/roles/bootstrap/README.md`):

```sh
ssh ansible@<host> sudo passwd pvginkel
```

## When things go wrong

- **`ssh-keyscan returned no ed25519 host key`**: target serves only RSA/ECDSA. Add an ed25519 key (`ssh-keygen -A` on the target generates the missing types) and rerun.
- **Play 2 fails with host-key verification error**: the line written by Play 1 doesn't match what SSH sees on connect. Inspect `files/known_hosts.d/<basename>` — the line should be `<host>,<host>.home ssh-ed25519 <keydata>`. Verify the target hasn't rotated its key between the keyscan and the connect.
- **`Permission denied` on Play 2**: connection user can't sudo. With `-u pvginkel`, ensure `-K` is set and the password is right. With `-u root`, confirm `ssh-add -l` lists the key and the agent is being forwarded.
- **`adoption_targets resolved to no hosts`**: pattern didn't match anything in inventory. Run `ansible-inventory -i inventories/<inv> --graph` to confirm the host or group exists.
