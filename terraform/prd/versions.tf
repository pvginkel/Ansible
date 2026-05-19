terraform {
  # Provider version constraint and required Terraform CLI version are
  # declared once in ../modules/managed-vm/versions.tf and inherited.
  # This block exists only to bind the local "proxmox" name to bpg/proxmox
  # for the `provider "proxmox" {}` instantiation in providers.tf.
  required_providers {
    proxmox = {
      source = "bpg/proxmox"
    }
    # tls backs the per-VM host keypair in main.tf. Adopted-only
    # configurations don't exercise it.
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    # local is retained only for the transitional `removed` block in
    # main.tf (the old known_hosts.d/prd writer). Drop it in the
    # ssh-host-ca cutover commit, with the `removed` block.
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
    homelab = {
      source = "pvginkel/homelab"
    }
  }
}
