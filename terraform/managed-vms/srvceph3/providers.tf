provider "proxmox" {
  endpoint  = var.proxmox_endpoint
  api_token = var.proxmox_api_token
  insecure  = var.proxmox_insecure

  # ssh { } block intentionally omitted — Phase 3a adoption modules do not
  # upload cloud-init snippets, so the provider has no need to SSH to PVE.
  # Added back when the VM gains a cloud-init resource at rebuild time.
}
