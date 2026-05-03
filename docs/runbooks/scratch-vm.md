# Scratch VM lifecycle

Scratch VMs are the disposable target for exercising Ansible roles. Spin them up, run a playbook against them, tear them down. Lost work on them is expected.

The fleet today is two microk8s scratch nodes (`wrkscratchk8s1`, `wrkscratchk8s2`) declared in `terraform/scratch/vms.tf`. Both run on `pve` and share one cloud-image download. The procedures below are written generically — substitute the relevant hostname (or use `--limit scratch` to target the whole fleet at once).

## Prerequisites (one-time)

1. Workstation set up per [`operator-workstation.md`](operator-workstation.md). Both SSH identities documented there are required: the operator key (Terraform uploads cloud-init snippets to `pve` over SSH as `root`) and the `ansible` service key (Ansible connects to each VM as `ansible` after cloud-init finishes).
2. Proxmox credentials set up per [`proxmox-credentials.md`](proxmox-credentials.md), with `terraform/scratch/terraform.tfvars` filled in.
3. On `pve`, **Snippets** content type enabled on the `local` datastore. Web UI → Datacenter → Storage → `local` → Edit → tick "Snippets" under Content.
4. dnsmasq reservations for each VM's pinned MAC. The deterministic MAC is `02:A7:F3:VV:VV:00` where `VV:VV` is the VMID big-endian (`03:85` for VMID 901, `03:86` for 902). Each reservation must exist *before* `terraform apply` so the first DHCP request gets the right answer.

## Create

```sh
cd terraform/scratch
terraform init     # first time only
terraform apply
```

First apply downloads the Ubuntu 24.04 cloud image (~600 MB) to `local`, uploads one cloud-init snippet per VM, and boots the VMs. Cloud-init takes another ~30 seconds per VM after Proxmox reports the VM running.

`terraform apply` also writes each VM's pinned ed25519 host key out to `ansible/files/known_hosts.d/scratch` (one combined file). **Commit that file** — Ansible reads it via `UserKnownHostsFile` to verify each VM's identity, and CI containers depend on it being in the repo. The diff appears whenever Terraform regenerates a host key (i.e. on first apply, or after explicitly tainting a `tls_private_key` resource).

To recreate just one VM (keeping the image and the other VMs intact):

```sh
terraform apply -replace='proxmox_virtual_environment_vm.scratch["wrkscratchk8s1"]'
```

Cloud-init only runs on first boot, so any change that needs to land via cloud-init (host key rotation, user-data edits) requires `-replace` on the VM resource above.

Poll for SSH readiness:

```sh
for h in wrkscratchk8s1 wrkscratchk8s2; do
  until ssh -o ConnectTimeout=2 -o StrictHostKeyChecking=accept-new ansible@$h true 2>/dev/null; do sleep 2; done
  echo "$h is up"
done
```

## Bootstrap + baseline (check mode first)

From the `ansible/` directory, against the whole scratch fleet:

```sh
cd ../../ansible
poetry run ansible-playbook playbooks/site.yml -i inventories/scratch --limit scratch --check --diff
```

Or against a single host:

```sh
poetry run ansible-playbook playbooks/site.yml -i inventories/scratch --limit wrkscratchk8s1 --check --diff
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

Per VM, `pvginkel` needs a sudo password set (see `ansible/roles/bootstrap/README.md` for why):

```sh
for h in wrkscratchk8s1 wrkscratchk8s2; do
  ssh ansible@$h sudo passwd pvginkel
done
```

## Verify idempotency

Re-run the playbook:

```sh
poetry run ansible-playbook playbooks/site.yml -i inventories/scratch --limit scratch
```

Expected: `PLAY RECAP` shows `changed=0` for every host. If anything reports changed on a second run, it's a role bug — fix the role before moving on.

## Destroy

```sh
cd ../terraform/scratch
terraform destroy
```

Both VMs, their cloud-init snippets, and the downloaded cloud image (if unused elsewhere) are removed. Recreating is idempotent — the pinned MACs mean dnsmasq hands the same IPs back, and the matching DNS entries follow.

## When things go wrong

- **Cloud-init never finishes** on a given VM: open a serial console in the Proxmox web UI (Datacenter → pve → `<vm-name>` → Console → xterm.js) and look at `/var/log/cloud-init.log` + `/var/log/cloud-init-output.log` inside the VM.
- **SSH as ansible fails with `Permission denied (publickey)`**: the cloud-init user-data didn't install the key. Confirm the snippet file at `/var/lib/vz/snippets/<vm-name>-user-data.yaml` on `pve` has your ed25519 key. `terraform apply` re-uploads it if edited.
- **Ansible run hangs on an apt task**: `NEEDRESTART_MODE=a` is set in the role to avoid the kernel-restart prompt; if it still hangs, something has a different prompt. SSH in and run the apt command manually to see what it's waiting on.
- **DNS can't resolve a scratch host**: the operator's `/etc/resolv.conf` must include `home` in its search domains. Verify with `resolvectl status` or equivalent.
- **`No route to host` from the playbook**: the VM IP isn't reachable. Check it's actually running (`qm status <vmid>` on `pve`), check the VM picked up a lease (serial console: `ip a` should show the reserved address), and verify nothing else on the LAN is squatting on it. If the VM has a different IP than expected, the dnsmasq reservation is missing or doesn't match the pinned MAC.
- **`REMOTE HOST IDENTIFICATION HAS CHANGED` from a manual `ssh`** (not the playbook): your personal `~/.ssh/known_hosts` is out of date. Ansible doesn't use it — playbook runs read `ansible/files/known_hosts.d/scratch`, which Terraform keeps in sync. For interactive sessions, either wipe the stale entry (`ssh-keygen -R <vm-name> && ssh-keygen -R <vm-name>.home`) or point your shell at the same repo file (`ssh -o UserKnownHostsFile=ansible/files/known_hosts.d/scratch ansible@<vm-name>`).
- **Playbook says `Host key verification failed`**: `ansible/files/known_hosts.d/scratch` is missing or out of date in the working copy. Run `terraform apply` (it'll regenerate the file from state without changing the VMs) and commit the result.
