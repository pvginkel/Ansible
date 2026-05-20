provider "proxmox" {
  endpoint = var.proxmox_endpoint
  username = var.proxmox_username
  password = var.proxmox_password
  insecure = var.proxmox_insecure

  # Snippet uploads (proxmox_virtual_environment_file content_type =
  # snippets) go over SSH, not the API. Required from phase 4b
  # onwards: from-scratch VMs ship a cloud-init snippet to PVE.
  # `agent = true` means the loaded ssh-agent must hold a key
  # authorized as `root` on each PVE node; ~/.ssh/config is ignored.
  # See docs/runbooks/operator-workstation.md.
  #
  # Per-node `address` overrides: the bpg client would otherwise read
  # each node's IP from the Proxmox cluster API and SSH there. The
  # `ssh_host_cert` role issues a host certificate whose principals
  # are `[<short>, <short>.home]` (no IPs); SSH's principal check
  # then rejects an IP-based connect with "principal <ip> not in
  # the set of valid principals". Force the connect target to the
  # `.home` FQDN so the principal check sees a matching name.
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
