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
}

variable "vm_name" {
  description = "Hostname / VM name. DNS entry <vm_name>.home must point at vm_ipv4_address."
  type        = string
  default     = "wrkscratch"
}

variable "vm_cpu_cores" {
  description = "vCPU cores."
  type        = number
  default     = 2
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

variable "vm_ipv4_address" {
  description = "Static IPv4 with CIDR (e.g. 10.1.0.34/24)."
  type        = string
  default     = "10.1.0.34/24"
}

variable "vm_ipv4_gateway" {
  description = "Default IPv4 gateway."
  type        = string
  default     = "10.1.0.1"
}

variable "vm_dns_servers" {
  description = "DNS servers pushed via cloud-init."
  type        = list(string)
  default     = ["10.2.1.2", "10.2.1.3"]
}

variable "vm_dns_domain" {
  description = "DNS search domain pushed via cloud-init."
  type        = string
  default     = "home"
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
