terraform {
  required_version = ">= 1.7.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.66"
    }
    # No version pin: no image bakes this provider. Every image that runs
    # Terraform here (iac, modern-app-dev, the argocd-hook) points
    # TF_CLI_CONFIG_FILE at an /etc/terraform.rc whose provider_installation
    # block routes registry.terraform.io/pvginkel/* to the tfmirror.home
    # network mirror, so what the mirror serves is the version source of
    # truth. See
    # /work/AnsibleSpecs/slices/completed/tf-provider-registry.md.
    homelab = {
      source = "pvginkel/homelab"
    }
  }
}
