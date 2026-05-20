provider "proxmox" {
  endpoint = var.proxmox_endpoint
  username = var.proxmox_username
  password = var.proxmox_password
  insecure = var.proxmox_insecure

  # The provider uses SSH (not the API) to upload snippets, so this block is
  # required whenever a `proxmox_virtual_environment_file` with
  # `content_type = "snippets"` is present. `agent = true` means the loaded
  # ssh-agent must hold a key authorized as `root` on each PVE node;
  # ~/.ssh/config is ignored. See docs/runbooks/operator-workstation.md.
  #
  # Per-node `address` overrides mirror terraform/prd/providers.tf —
  # the `ssh_host_cert` role's certificate principals are short + .home
  # only, so the bpg client must connect by FQDN (not the API-supplied
  # IP) for SSH's principal check to pass.
  ssh {
    agent    = true
    username = "root"

    node {
      name    = "pve"
      address = "pve.home"
    }
    node {
      name    = "pve1"
      address = "pve1.home"
    }
    node {
      name    = "pve2"
      address = "pve2.home"
    }
  }
}

provider "homelab" {
  dns_reservation_url   = var.dns_reservation_url
  dns_reservation_token = var.dns_reservation_token
}
