terraform {
  # Provider version constraint and required Terraform CLI version are
  # declared once in ../modules/managed-vm/versions.tf and inherited.
  # This block exists only to bind the local "proxmox" name to bpg/proxmox
  # for the `provider "proxmox" {}` instantiation in providers.tf.
  required_providers {
    proxmox = {
      source = "bpg/proxmox"
    }
    # tls + local providers are used by the from-scratch resources in
    # main.tf (per-VM host keypair + known_hosts.d/prd). Adopted-only
    # configurations don't exercise them.
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}
