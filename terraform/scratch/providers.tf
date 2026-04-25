provider "proxmox" {
  endpoint  = var.proxmox_endpoint
  api_token = var.proxmox_api_token
  insecure  = var.proxmox_insecure

  # The provider uses SSH (not the API) to upload snippets, so this block is
  # required whenever a `proxmox_virtual_environment_file` with
  # `content_type = "snippets"` is present. `agent = true` means the loaded
  # ssh-agent must hold a key authorized as `root` on each PVE node;
  # ~/.ssh/config is ignored. See docs/runbooks/operator-workstation.md.
  ssh {
    agent    = true
    username = "root"
  }
}
