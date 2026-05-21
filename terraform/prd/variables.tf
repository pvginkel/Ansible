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

variable "dns_reservation_url" {
  description = "Base URL of the dnsmasq sidecar reservation API (e.g. https://dns-reservation.home/). See /work/AnsibleSpecs/slices/completed/dns-reservation-provider/dns-reservation-api.md."
  type        = string
}

variable "dns_reservation_token" {
  description = "Bearer token for the dnsmasq sidecar reservation API. Lives in terraform.tfvars (gitignored)."
  type        = string
  sensitive   = true
}

variable "backup_server_url" {
  description = "Base URL of the in-cluster backup-server (e.g. https://backup-server.home/). See /work/DockerImages/backup-server/upload-api.md."
  type        = string
}

variable "backup_server_token" {
  description = "Management bearer token for the backup-server admin API; authorizes minting + destroying scope-bound upload credentials. Source: backupServer.managementToken in HelmCharts configs/prd/storage-values.yaml. Lives in terraform.tfvars (gitignored)."
  type        = string
  sensitive   = true
}
