# Scratch VM lifecycle

The scratch VM is the disposable target for exercising Ansible roles. Spin it up, run a playbook against it, tear it down. Lost work on it is expected.

## Prerequisites (one-time)

1. Workstation set up per [`operator-workstation.md`](operator-workstation.md). Both SSH identities documented there are required: the operator key (Terraform uploads cloud-init snippets to `pve` over SSH as `root`) and the `ansible` service key (Ansible connects to the VM as `ansible` after cloud-init finishes).
2. Proxmox API token created per [`proxmox-api-token.md`](proxmox-api-token.md), with `terraform/scratch/terraform.tfvars` filled in.
3. On `pve`, **Snippets** content type enabled on the `local` datastore. Web UI → Datacenter → Storage → `local` → Edit → tick "Snippets" under Content.
4. dnsmasq reservation for the scratch VM's MAC (`02:A7:F3:03:84:00` for the default VMID 900) pointing `wrkscratch.home` at the desired IP. The reservation must exist *before* `terraform apply` so the first DHCP request gets the right answer.

## Create

```sh
cd terraform/scratch
terraform init     # first time only
terraform apply
```

First apply downloads the Ubuntu 24.04 cloud image (~600 MB) to `local`, uploads the cloud-init snippet, and boots the VM. Cloud-init takes another ~30 seconds after Proxmox reports the VM running.

`terraform apply` also writes the VM's pinned ed25519 host key out to `ansible/files/known_hosts.d/scratch`. **Commit that file** — Ansible reads it via `UserKnownHostsFile` to verify the VM's identity, and CI containers depend on it being in the repo. The diff appears whenever Terraform regenerates the host key (i.e. on first apply, or after explicitly tainting the `tls_private_key` resource).

To recreate just the VM (keeping the image), run:

```sh
terraform apply -replace=proxmox_virtual_environment_vm.scratch
```

Cloud-init only runs on first boot, so any change that needs to land via cloud-init (host key rotation, user-data edits) requires `-replace` on the VM resource above.

Poll for SSH readiness:

```sh
until ssh -o ConnectTimeout=2 -o StrictHostKeyChecking=accept-new ansible@wrkscratch true 2>/dev/null; do sleep 2; done
echo "wrkscratch is up"
```

## Bootstrap + baseline (check mode first)

From the `ansible/` directory:

```sh
cd ../../ansible
poetry run ansible-playbook playbooks/site.yml -i inventories/scratch --limit scratch --check --diff
```

Review the diff. It should show:
- `pvginkel` user being created (on the first run)
- SSH key installed for `pvginkel`
- timezone, motd scripts, vimrc, unattended-upgrades, qemu-guest-agent tasks

If the diff looks right, apply for real:

```sh
poetry run ansible-playbook playbooks/site.yml -i inventories/scratch --limit scratch
```

## One-time post-bootstrap step

`pvginkel` needs a sudo password set (see `ansible/roles/bootstrap/README.md` for why):

```sh
ssh ansible@wrkscratch sudo passwd pvginkel
```

## Verify idempotency

Re-run the playbook:

```sh
poetry run ansible-playbook playbooks/site.yml -i inventories/scratch --limit scratch
```

Expected: `PLAY RECAP` shows `changed=0`. If anything reports changed on a second run, it's a role bug — fix the role before moving on.

## Destroy

```sh
cd ../terraform/scratch
terraform destroy
```

VM, cloud-init snippet, and the downloaded cloud image (if unused elsewhere) are removed. Recreating is idempotent — the pinned MAC means dnsmasq hands the same IP back, and the matching DNS entry follows.

## When things go wrong

- **Cloud-init never finishes**: open a serial console in the Proxmox web UI (Datacenter → pve → wrkscratch → Console → xterm.js) and look at `/var/log/cloud-init.log` + `/var/log/cloud-init-output.log` inside the VM.
- **SSH as ansible fails with `Permission denied (publickey)`**: the cloud-init user-data didn't install the key. Confirm the snippet file at `/var/lib/vz/snippets/wrkscratch-user-data.yaml` on `pve` has your ed25519 key. `terraform apply` re-uploads it if edited.
- **Ansible run hangs on an apt task**: `NEEDRESTART_MODE=a` is set in the role to avoid the kernel-restart prompt; if it still hangs, something has a different prompt. SSH in and run the apt command manually to see what it's waiting on.
- **DNS can't resolve `wrkscratch`**: the operator's `/etc/resolv.conf` must include `home` in its search domains. Verify with `resolvectl status` or equivalent.
- **`No route to host` from the playbook**: the VM IP isn't reachable. Check it's actually running (`qm status <vmid>` on `pve`), check the VM picked up a lease (serial console: `ip a` should show the reserved address), and verify nothing else on the LAN is squatting on it. If the VM has a different IP than expected, the dnsmasq reservation is missing or doesn't match the pinned MAC.
- **`REMOTE HOST IDENTIFICATION HAS CHANGED` from a manual `ssh`** (not the playbook): your personal `~/.ssh/known_hosts` is out of date. Ansible doesn't use it — playbook runs read `ansible/files/known_hosts.d/scratch`, which Terraform keeps in sync. For interactive sessions, either wipe the stale entry (`ssh-keygen -R wrkscratch && ssh-keygen -R wrkscratch.home`) or point your shell at the same repo file (`ssh -o UserKnownHostsFile=ansible/files/known_hosts.d/scratch ansible@wrkscratch`).
- **Playbook says `Host key verification failed`**: `ansible/files/known_hosts.d/scratch` is missing or out of date in the working copy. Run `terraform apply` (it'll regenerate the file from state without changing the VM) and commit the result.
