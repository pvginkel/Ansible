variable "proxmox_endpoint" {
  description = "Proxmox API endpoint, including scheme and port (e.g. https://pve.home:8006/)."
  type        = string
}

variable "proxmox_api_token" {
  description = "Proxmox API token in the form 'user@realm!tokenid=secret'."
  type        = string
  sensitive   = true
}

variable "proxmox_insecure" {
  description = "Skip TLS verification against the Proxmox endpoint. Proxmox ships with a self-signed cert by default."
  type        = bool
  default     = true
}

variable "pve_node" {
  description = "Proxmox node that hosts the scratch VM."
  type        = string
  default     = "pve"
}

variable "vm_id" {
  description = "Proxmox VMID for the scratch VM."
  type        = number
  default     = 900

  # MAC encoding allots two bytes to the VMID, so values past 65535 would
  # silently truncate and collide. Catch that at plan time instead.
  validation {
    condition     = var.vm_id >= 100 && var.vm_id <= 65535
    error_message = "vm_id must be in [100, 65535] so it fits in two bytes of the MAC encoding."
  }
}

variable "vm_name" {
  description = "Hostname / VM name. dnsmasq must hold a reservation for the VM's MAC pointing <vm_name>.home at the desired IP."
  type        = string
  default     = "wrkscratch"
}

variable "vm_cpu_cores" {
  description = "vCPU cores."
  type        = number
  default     = 2
}

variable "vm_cpu_affinity" {
  description = "Host CPU affinity for the VM (Proxmox 'affinity' field, e.g. '0-11' for fast cores). Empty disables pinning. Only meaningful on hosts whose topology you've zoned; pve uses 0-11 (interactive) and 12-19 (background)."
  type        = string
  default     = ""
}

variable "vm_memory_mb" {
  description = "RAM in MiB."
  type        = number
  default     = 4096
}

variable "vm_disk_size_gb" {
  description = "Boot disk size in GiB. Cloud image is resized on import."
  type        = number
  default     = 20
}

variable "vm_backup" {
  description = "Whether vzdump should include this VM's boot disk in cluster backups."
  type        = bool
  default     = true
}

variable "vm_storage" {
  description = "Proxmox storage pool for the VM disk."
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
  description = "Proxmox datastore that holds the downloaded cloud image and the cloud-init snippet. Must have both 'iso' and 'snippets' content types enabled."
  type        = string
  default     = "local"
}

variable "ansible_ssh_public_key" {
  description = "Public key the cloud-init process installs for the ansible user. Empty (default) reads from ansible/roles/bootstrap/files/ansible.pub so it stays in sync with the role."
  type        = string
  default     = ""
}
