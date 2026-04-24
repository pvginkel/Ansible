provider "proxmox" {
  endpoint  = var.proxmox_endpoint
  api_token = var.proxmox_api_token
  insecure  = var.proxmox_insecure

  # SSH is a fallback path the provider uses for a handful of operations
  # (e.g. specific disk import flows). Snippets and VM lifecycle go over the
  # API with the token above.
  ssh {
    agent    = true
    username = "root"
  }
}
