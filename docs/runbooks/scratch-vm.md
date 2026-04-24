# Scratch VM lifecycle

The scratch VM is the disposable target for exercising Ansible roles. Spin it up, run a playbook against it, tear it down. Lost work on it is expected.

## Prerequisites (one-time)

1. Proxmox API token created per [`proxmox-api-token.md`](proxmox-api-token.md), with `terraform/scratch/terraform.tfvars` filled in.
2. On `pve`, **Snippets** content type enabled on the `local` datastore. Web UI → Datacenter → Storage → `local` → Edit → tick "Snippets" under Content.
3. DNS A record `scratch.home` → `10.1.0.34` (already in place).
4. Operator SSH config knows where to find the `ansible` private key. Add to `~/.ssh/config` on `wrkdev`:

   ```
   Host scratch scratch.home
     User ansible
     IdentityFile ~/.ssh/id_ed25519_ansible
     IdentitiesOnly yes
   ```

   (Place the private key at that path after restoring from Roboform / the cloud folder.)

## Create

```sh
cd terraform/scratch
terraform init     # first time only
terraform apply
```

First apply downloads the Ubuntu 24.04 cloud image (~600 MB) to `local`, uploads the cloud-init snippet, and boots the VM. Cloud-init takes another ~30 seconds after Proxmox reports the VM running.

Poll for SSH readiness:

```sh
until ssh -o ConnectTimeout=2 -o StrictHostKeyChecking=accept-new ansible@scratch true 2>/dev/null; do sleep 2; done
echo "scratch is up"
```

## Bootstrap + baseline (check mode first)

From the `ansible/` directory:

```sh
cd ../../ansible
poetry run ansible-playbook playbooks/site.yml -i inventories/dev --limit scratch --check --diff
```

Review the diff. It should show:
- `pvginkel` user being created (on the first run)
- SSH key installed for `pvginkel`
- timezone, motd scripts, vimrc, unattended-upgrades, qemu-guest-agent tasks

If the diff looks right, apply for real:

```sh
poetry run ansible-playbook playbooks/site.yml -i inventories/dev --limit scratch
```

## One-time post-bootstrap step

`pvginkel` needs a sudo password set (see `ansible/roles/bootstrap/README.md` for why):

```sh
ssh ansible@scratch sudo passwd pvginkel
```

## Verify idempotency

Re-run the playbook:

```sh
poetry run ansible-playbook playbooks/site.yml -i inventories/dev --limit scratch
```

Expected: `PLAY RECAP` shows `changed=0`. If anything reports changed on a second run, it's a role bug — fix the role before moving on.

## Destroy

```sh
cd ../terraform/scratch
terraform destroy
```

VM, cloud-init snippet, and the downloaded cloud image (if unused elsewhere) are removed. Recreating is idempotent — same IP, same hostname, same DNS entry.

## When things go wrong

- **Cloud-init never finishes**: open a serial console in the Proxmox web UI (Datacenter → pve → scratch → Console → xterm.js) and look at `/var/log/cloud-init.log` + `/var/log/cloud-init-output.log` inside the VM.
- **SSH as ansible fails with `Permission denied (publickey)`**: the cloud-init user-data didn't install the key. Confirm the snippet file at `/var/lib/vz/snippets/scratch-user-data.yaml` on `pve` has your ed25519 key. `terraform apply` re-uploads it if edited.
- **Ansible run hangs on an apt task**: `NEEDRESTART_MODE=a` is set in the role to avoid the kernel-restart prompt; if it still hangs, something has a different prompt. SSH in and run the apt command manually to see what it's waiting on.
- **DNS can't resolve `scratch`**: the operator's `/etc/resolv.conf` must include `home` in its search domains. Verify with `resolvectl status` or equivalent.
