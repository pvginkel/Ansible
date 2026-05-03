variable "proxmox_endpoint" {
  description = "Proxmox API endpoint, including scheme and port (e.g. https://pve.home:8006/)."
  type        = string
}

variable "proxmox_username" {
  description = "Proxmox login (realm-qualified, e.g. root@pam). See decisions.md \"Proxmox VM CPU affinity\" for why this is root@pam rather than a scoped API token."
  type        = string
  default     = "root@pam"
}

variable "proxmox_password" {
  description = "Password for proxmox_username. Lives in terraform.tfvars (gitignored)."
  type        = string
  sensitive   = true
}

variable "proxmox_insecure" {
  description = "Skip TLS verification against the Proxmox endpoint. Proxmox ships with a self-signed cert by default."
  type        = bool
  default     = true
}
