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

variable "pve_node" {
  description = "PVE node that the cloud image is downloaded to. Per-VM `pve_node` in the `vms` locals map controls placement; this variable only governs the one image-download resource (a single download serves all VMs that live on the same node)."
  type        = string
  default     = "pve"
}

variable "vm_storage" {
  description = "Proxmox storage pool for the VM disks (root, EFI, cloud-init)."
  type        = string
  default     = "local-lvm"
}

variable "vm_bridge" {
  description = "Proxmox bridge for the VM NIC."
  type        = string
  default     = "vmbr0"
}

variable "ubuntu_cloud_image_url" {
  description = "Source URL for the Ubuntu cloud image. 'current' tracks the latest LTS point release."
  type        = string
  default     = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
}

variable "image_datastore" {
  description = "Proxmox datastore that holds the downloaded cloud image and the cloud-init snippets. Must have both 'iso' and 'snippets' content types enabled."
  type        = string
  default     = "local"
}

variable "ansible_ssh_public_key" {
  description = "Public key the cloud-init process installs for the ansible user. Empty (default) reads from ansible/roles/bootstrap/files/ansible.pub so it stays in sync with the role."
  type        = string
  default     = ""
}
