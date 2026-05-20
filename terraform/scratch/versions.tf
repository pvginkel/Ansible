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
    homelab = {
      source = "pvginkel/homelab"
    }
  }
}
