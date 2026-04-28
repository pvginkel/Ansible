# Operator workstation setup

The machine that runs Terraform + Ansible against the homelab — today, `wrkdev`. Everything here is one-time setup; nothing in this file should need re-running on a regular basis.

## Python + Poetry

- Python 3.12+
- [Poetry](https://python-poetry.org/) 2.x

```sh
poetry install
poetry run ansible-galaxy collection install -r ansible/collections/requirements.yml
```

Poetry creates an in-project `.venv/` (configured via `poetry.toml`). Prefix commands with `poetry run` or activate the venv (`source .venv/bin/activate`).

## SSH keys and ssh-agent

Two distinct identities are in play. Keep them separate.

### `pve-root` key — used by Terraform to reach PVE as `root`

The `bpg/proxmox` provider uploads cloud-init snippets over SSH (the Proxmox API has no snippets endpoint), authenticating as `root` on the target PVE node. It reads the key from `ssh-agent` only — `~/.ssh/config` is ignored, so a `User`/`IdentityFile` mapping there will not help.

Dedicated keypair: `id_ed25519_pve` (private half in the cloud-synced attachments folder; public half tracked in this repo at [`ansible/files/pve.pub`](../../ansible/files/pve.pub)). Restore the private key onto this workstation at `~/.ssh/id_ed25519_pve` (`chmod 600`).

One-time install on each PVE node — append `pve.pub` to `/root/.ssh/authorized_keys` on `pve`, `pve1`, `pve2`. Easiest path is to paste it via the Proxmox web shell on each node.

Per-shell: load it into the agent.

```sh
ssh-add ~/.ssh/id_ed25519_pve
ssh-add -L                       # confirm "pve-root" is listed
ssh -o IdentitiesOnly=no root@pve true   # exit 0 → terraform's SSH will work
```

### `ansible` service key — used by Ansible to reach managed VMs as `ansible`

`ansible/roles/bootstrap/files/ansible.pub` is the public half of a dedicated keypair owned by Ansible-the-tool. Cloud-init seeds it onto every managed VM as the `ansible` user; playbooks then connect as that user.

Private key is `id_ed25519_ansible` in the same attachments folder; restore to `~/.ssh/id_ed25519_ansible` and tell SSH about it:

```
# ~/.ssh/config
Host wrkscratch* wrkscratch*.home k8s* ceph*
  User ansible
  IdentityFile ~/.ssh/id_ed25519_ansible
  IdentitiesOnly yes
```

(Adjust the host pattern as more managed hosts come online. PVE nodes are not in the list — they're reached as `root` via the `pve-root` key, not as `ansible`.)

### Why not reuse one key for both paths?

The two identities have different lifecycles and blast radii. The `ansible` key is sprayed onto every managed Ubuntu VM and committed to the repo (public half); rotating it is a fleet-wide operation. The operator key authorizes a human at the keyboard against PVE's root account; rotating it is a couple of `authorized_keys` edits. Folding them together means an `ansible`-key rotation also breaks Terraform, and a re-keying of `root@pve*` also breaks playbook runs on managed VMs. Cheap to keep separate; expensive to disentangle later.

## DNS

The `.home` search domain must be present in `/etc/resolv.conf` (or the systemd-resolved equivalent). Verify with `resolvectl status`. Without it, short hostnames like `pve`, `srvk8sl1` will not resolve.

## Proxmox API token

Required for Terraform's API path (everything that isn't snippet upload). Follow [`proxmox-api-token.md`](proxmox-api-token.md) once per fresh PVE cluster; the resulting token goes in `terraform/scratch/terraform.tfvars` (gitignored).
