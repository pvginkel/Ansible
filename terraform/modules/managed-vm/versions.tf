terraform {
  required_version = ">= 1.7.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.66"
    }
    # No version pin: provider binary is baked into the modern-app-dev
    # image, which is the version source of truth. See
    # /work/AnsibleSpecs/slices/completed/embed-homelab-provider.md.
    homelab = {
      source = "pvginkel/homelab"
    }
  }
}
