terraform {
  required_version = ">= 1.7.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.66"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    # local is retained only for the transitional `removed` block in
    # main.tf. Drop it in the ssh-host-ca cutover commit.
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
    homelab = {
      source = "pvginkel/homelab"
    }
  }
}
