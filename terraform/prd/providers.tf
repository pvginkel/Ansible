provider "proxmox" {
  endpoint  = var.proxmox_endpoint
  api_token = var.proxmox_api_token
  insecure  = var.proxmox_insecure

  # Snippet uploads (proxmox_virtual_environment_file content_type =
  # snippets) go over SSH, not the API. Required from phase 4b
  # onwards: from-scratch VMs ship a cloud-init snippet to PVE.
  # `agent = true` means the loaded ssh-agent must hold a key
  # authorized as `root` on each PVE node; ~/.ssh/config is ignored.
  # See docs/runbooks/operator-workstation.md.
  ssh {
    agent    = true
    username = "root"
  }
}
