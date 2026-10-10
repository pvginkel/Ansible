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

The homelab host CA in `~/.ssh/known_hosts`: the bpg provider has its own SSH client and reads `~/.ssh/known_hosts` (the system default — Ansible's `UserKnownHostsFile=files/known_hosts.d/homelab` doesn't apply to it). PVE nodes serve step-ca-signed host certificates via the `ssh_host_cert` role, so the workstation needs the homelab CA's `@cert-authority` line in `~/.ssh/known_hosts` or the provider rejects the handshake with `ssh: no authorities for hostname`. Run, from the checkout:

```sh
./scripts/kubecoder-keys.sh
```

With none of its key variables set, the script writes no key, only the `@cert-authority` lines of `ansible/files/known_hosts.d/homelab`, and it removes a homelab CA line that file no longer carries. Run it again whenever that file changes, as an [SSH host CA rotation](ssh-host-ca-rotation.md) does twice.

### `ansible` service key — used by Ansible to reach managed VMs as `ansible`

`ansible/roles/bootstrap/files/ansible.pub` is the public half of a dedicated keypair owned by Ansible-the-tool. Cloud-init seeds it onto every managed VM as the `ansible` user; playbooks then connect as that user.

Where the private half comes from depends on whether SecretRotator's `ssh-key` kind is in its nightly run's `kinds_enabled` (`src/secret_rotator/switches.yaml` on its `prd`). Until it is, the private key is `id_ed25519_ansible` in the same attachments folder, and in RoboForm. Once it is, the kind replaces the key every 14 days, commits each new public half to `ansible.pub`, and the attachments folder and RoboForm hold no copy. The private half is then `kv/iac/ansible-ssh-key#private` in OpenBao. A KubeCoder environment gets it from the catalog at each start (`scripts/kubecoder-keys.sh`). A workstation takes it from OpenBao, and again after each rotation:

```sh
(umask 077; bao kv get -mount=kv -field=private iac/ansible-ssh-key > ~/.ssh/id_ed25519_ansible)
```

With OpenBao down, srviac keeps its own copy ([`iac-cold-boot.md`](iac-cold-boot.md)), and a person's way in is the `pvginkel` account.

Restore it to `~/.ssh/id_ed25519_ansible` and tell SSH about it:

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

## ansible-vault passphrase

A few fleet secrets are ansible-vault'd in the repo — among them the shared VRRP password and the step-ca JWK provisioner password in `ansible/inventories/prd/group_vars/all/vips.yml`, the OpenBao seal key (`ansible/roles/openbao/files/static.key`), and step-ca's keys, passwords and configuration in `ansible/roles/step_ca/files/`. One passphrase decrypts all of them; the source of truth is Roboform.

Cache it on this workstation in `ansible/.vault_pass` — a single line, gitignored:

```sh
printf '%s' '<passphrase-from-Roboform>' > ansible/.vault_pass
chmod 600 ansible/.vault_pass
```

Point Ansible at it through the **environment**, not `ansible.cfg`: export `ANSIBLE_VAULT_PASSWORD_FILE` (absolute path to `ansible/.vault_pass`) from your shell profile. `ansible-playbook`, `ansible-vault`, and friends then decrypt non-interactively — no `--ask-vault-pass`, no per-run flag.

`vault_password_file` is deliberately **not** set in `ansible.cfg`. That would make the file mandatory for *every* run — CI included, and any host without the passphrase — even plays that touch no vault content (Ansible resolves the configured file eagerly the moment it parses a `!vault` tag, and a missing file is fatal). Keeping the pointer in the environment makes it opt-in: only runs that actually read a vault'd variable need it.

CI and scheduled drift run `site.yml`, which reads no vault'd variable, so they need nothing. The iac agent VM materialises `.vault_pass` via its own secret bootstrap (`/etc/iac/secrets.yaml`) for the day a CI play does consume one.

## DNS

The `.home` search domain must be present in `/etc/resolv.conf` (or the systemd-resolved equivalent). Verify with `resolvectl status`. Without it, short hostnames like `pve`, `srvk8sl1` will not resolve.

## Proxmox credentials

Terraform authenticates to the Proxmox API as `root@pam` with username + password — see [`proxmox-credentials.md`](proxmox-credentials.md). The password goes in `terraform/{prd,scratch}/terraform.tfvars` (gitignored).

## Terraform `pvginkel/homelab` provider

The `homelab` provider is served from the private network mirror at `https://tfmirror.home/`. Three images set `TF_CLI_CONFIG_FILE=/etc/terraform.rc` and carry a byte-identical copy of that file, and that file's `provider_installation` block routes `registry.terraform.io/pvginkel/*` to the mirror and excludes it from `direct` — so `terraform init` installs `pvginkel/homelab` from there, with a real lockfile hash and no per-workstation setup. No binary is baked into any of them. Readdressing `tfmirror.home` means editing all three copies:

- [`support/iac-image/terraform.rc`](../../support/iac-image/terraform.rc) — the `iac` image
- `/work/DockerImages/kube-coder-dev-base/terraform.rc` — the KubeCoder dev base image; the `iac` sidecar (`kube-coder-iac-toolchain`) inherits this copy
- `/work/ArgoCDTools/argocd-hook/image/terraform.rc` — the Argo CD Terraform PreSync hook image

See [`/work/AnsibleSpecs/slices/completed/tf-provider-registry.md`](../../../AnsibleSpecs/slices/completed/tf-provider-registry.md) for the mirror and how each provider version is published to it.

Inside those containers no `~/.terraformrc` is needed; a Terraform run outside them needs the same `network_mirror` block in `~/.terraformrc`. If a stale dev-override block is still present from plan 02, delete it — it is harmless inside the container (the env var wins), but it fires elsewhere.

The bearer token for the sidecar API goes in `terraform/{prd,scratch}/terraform.tfvars` next to the Proxmox password (`dns_reservation_token`).
