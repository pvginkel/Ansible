terraform {
  # Provider version constraint and required Terraform CLI version are
  # declared once in ../modules/managed-vm/versions.tf and inherited.
  # This block exists only to bind the local "proxmox" name to bpg/proxmox
  # for the `provider "proxmox" {}` instantiation in providers.tf.
  required_providers {
    proxmox = {
      source = "bpg/proxmox"
    }
  }
}
