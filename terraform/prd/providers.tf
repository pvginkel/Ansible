provider "proxmox" {
  endpoint  = var.proxmox_endpoint
  api_token = var.proxmox_api_token
  insecure  = var.proxmox_insecure

  # No ssh { } block — none of the VMs in local.vms upload cloud-init
  # snippets today, so the provider has no need to SSH to PVE. Add it back
  # when a VM here gains a cloud-init resource (see terraform/scratch/).
}
