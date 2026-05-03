# `terraform/scratch` — disposable scratch VMs

Multi-VM Terraform config for the disposable scratch fleet. Used to exercise Ansible roles end-to-end without risking a real host. The set is declared in [`vms.tf`](./vms.tf); today it holds two microk8s scratch nodes (Phase 4) — `wrkscratchk8s1` (VMID 901) and `wrkscratchk8s2` (VMID 902). VMID range 900-909 is reserved for scratch.

## Prerequisites

1. Proxmox credentials set up — see [`docs/runbooks/proxmox-credentials.md`](../../docs/runbooks/proxmox-credentials.md).
2. On `pve`, the `local` datastore must have both **ISO image** and **Snippets** content types enabled. Proxmox web UI → Datacenter → Storage → `local` → Edit → tick both under "Content". Default Proxmox enables ISO but not Snippets.
3. dnsmasq reservations for each VM's pinned MAC pointing `<vm-name>.home` at the desired IP — must exist *before* `terraform apply` so the first DHCP lease lands on the reserved address. See `docs/runbooks/scratch-vm.md` for the MAC values and the rest of the prereqs.

## First-time setup

```sh
cd terraform/scratch
cp terraform.tfvars.example terraform.tfvars
# Fill in proxmox_password.
terraform init
```

## Create the VMs

```sh
terraform plan
terraform apply
```

On first apply Terraform downloads the Ubuntu 24.04 cloud image (~600 MB) to `local`, uploads one cloud-init snippet per VM, then creates and boots each VM. Cloud-init creates the `ansible` user with NOPASSWD sudo and the ed25519 public key. Total time: ~1–2 minutes per VM plus the image download.

Once the VMs are up:

```sh
ssh ansible@wrkscratchk8s1.home    # key-based, no password
ssh ansible@wrkscratchk8s2.home
```

From there, Ansible takes over — see `ansible/playbooks/site.yml` and the scratch-VM runbook.

## Destroy

```sh
terraform destroy
```

Removes both VMs, their cloud-init snippets, and — if nothing else references it — the downloaded image. Destroy is intended to be a frequent operation: tear the cluster down as soon as you've validated a role change.

## Adding or removing scratch VMs

Edit `vms.tf` — add or remove an entry in the `local.vms` map. Each entry needs `vm_id`, `pve_node`, `description`, `tags`, `cpu_cores`, `memory_mb`, `disk_size_gb`. Allocate the `vm_id` from the 900-909 range; the deterministic MAC is computed from the VMID, so a new entry implies a new dnsmasq reservation before apply.

## Picking up a newer Ubuntu point release

The `ubuntu_cloud_image_url` uses Canonical's `current/` alias. Terraform's `ignore_changes` on `disk[0].file_id` means it won't rebuild the VMs automatically when Canonical publishes a new release. To pull a fresh image:

```sh
terraform taint 'proxmox_download_file.ubuntu_cloud_image'
terraform apply
# then recreate the VMs to pick up the new image:
terraform taint 'proxmox_virtual_environment_vm.scratch["wrkscratchk8s1"]'
terraform taint 'proxmox_virtual_environment_vm.scratch["wrkscratchk8s2"]'
terraform apply
```
