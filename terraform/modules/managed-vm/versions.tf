terraform {
  required_version = ">= 1.7.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.66"
    }
    # Dev-override only; no version pin. Provider built from
    # /work/HomelabTerraformProvider via ~/.terraformrc — see
    # docs/runbooks/operator-workstation.md.
    homelab = {
      source = "pvginkel/homelab"
    }
  }
}
