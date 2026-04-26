# `terraform/scratch` — disposable scratch VM

Single-VM Terraform config for Phase 1. Used to exercise the `bootstrap` and `baseline` Ansible roles end-to-end without risking a real host.

## Prerequisites

1. Proxmox API token set up — see [`docs/runbooks/proxmox-api-token.md`](../../docs/runbooks/proxmox-api-token.md).
2. On `pve`, the `local` datastore must have both **ISO image** and **Snippets** content types enabled. Proxmox web UI → Datacenter → Storage → `local` → Edit → tick both under "Content". Default Proxmox enables ISO but not Snippets.
3. dnsmasq reservation for the VM's pinned MAC pointing `wrkscratch.home` at the desired IP — must exist *before* `terraform apply` so the first DHCP lease lands on the reserved address. See `docs/runbooks/scratch-vm.md` for the MAC value and the rest of the prereqs.

## First-time setup

```sh
cd terraform/scratch
cp terraform.tfvars.example terraform.tfvars
# Fill in proxmox_api_token and ansible_ssh_public_key.
terraform init
```

## Create the VM

```sh
terraform plan
terraform apply
```

On first apply Terraform downloads the Ubuntu 24.04 cloud image (~600 MB) to `local`, uploads the cloud-init user-data as a snippet, then creates and boots the VM. Cloud-init creates the `ansible` user with NOPASSWD sudo and the ed25519 public key. Total time: ~1–2 minutes plus the image download.

Once the VM is up:

```sh
ssh ansible@wrkscratch.home    # key-based, no password
```

From there, Ansible takes over — see `ansible/playbooks/site.yml` and the scratch-VM runbook.

## Destroy

```sh
terraform destroy
```

Removes the VM, the cloud-init snippet, and — if nothing else references it — the downloaded image. Destroy is intended to be a frequent operation: tear the VM down as soon as you've validated a role change.

## Picking up a newer Ubuntu point release

The `ubuntu_cloud_image_url` uses Canonical's `current/` alias. Terraform's `ignore_changes` on `disk[0].file_id` means it won't rebuild the VM automatically when Canonical publishes a new release. To pull a fresh image:

```sh
terraform taint 'proxmox_download_file.ubuntu_cloud_image'
terraform apply
# then recreate the VM to pick up the new image:
terraform taint 'proxmox_virtual_environment_vm.scratch'
terraform apply
```
