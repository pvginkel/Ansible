variable "name" {
  description = "VM hostname / Proxmox VM name."
  type        = string
}

variable "description" {
  description = "Human-readable description written into qm.conf and visible in the PVE UI."
  type        = string
  default     = ""
}

variable "vm_id" {
  description = "Proxmox VMID. See decisions.md \"VMID convention\"."
  type        = number

  validation {
    condition     = var.vm_id >= 100 && var.vm_id <= 65535
    error_message = "vm_id must be in [100, 65535] so it fits in two bytes of the MAC encoding."
  }
}

variable "pve_node" {
  description = "PVE host that runs this VM."
  type        = string
  default     = "pve"
}

variable "tags" {
  description = "Tags applied to the VM in PVE. Conventional set: ansible-managed, terraform, plus a class tag (e.g. ceph, k8s)."
  type        = list(string)
  default     = ["ansible-managed", "terraform"]
}

variable "bios" {
  description = "BIOS mode. Default ovmf (UEFI); legacy VMs may still be seabios."
  type        = string
  default     = "ovmf"
}

variable "boot_order" {
  description = "Boot device order. Default matches an Ubuntu cloud install with the legacy ide2 cdrom drive present."
  type        = list(string)
  default     = ["scsi0", "ide2", "net0"]
}

variable "smbios_uuid" {
  description = "Pinned SMBIOS UUID. For imported VMs, copy from `qm config <vmid> | grep smbios1`. For new VMs, omit and let bpg generate."
  type        = string
  default     = null
}

variable "cpu_cores" {
  description = "vCPU cores per socket."
  type        = number
}

variable "cpu_sockets" {
  description = "vCPU socket count."
  type        = number
  default     = 1
}

variable "cpu_type" {
  description = "QEMU CPU type. \"host\" passes the underlying CPU's instruction set straight through; the right default for our Linux guests."
  type        = string
  default     = "host"
}

variable "memory_mb" {
  description = "Dedicated memory in MiB."
  type        = number
}

variable "managed_disks" {
  description = "Disks backed by a PVE storage pool (root, data). Backup flag is computed from `pve_node`'s host_vars."
  type = list(object({
    interface    = string
    size         = number
    datastore_id = optional(string, "local-lvm")
    discard      = optional(string, "on")
    iothread     = optional(bool, true)
  }))
  default = []
}

variable "passthrough_disks" {
  description = "Block devices passed through from the PVE host (Ceph OSDs, ZFS volumes). Always backup=false. See decisions.md \"Disk passthrough on managed VMs\" — TF cannot create or modify these via API token; existing passthroughs survive imports cleanly, but rebuilds need Ansible to reattach."
  type = list(object({
    interface         = string
    path_in_datastore = string
  }))
  default = []
}

variable "network_devices" {
  description = "VM NICs. Order matters — bpg state indexes by position, not by interface."
  type = list(object({
    bridge      = string
    mac_address = string
    vlan_id     = optional(number, 0)
    firewall    = optional(bool, true)
    model       = optional(string, "virtio")
  }))
}

variable "efi_disk_datastore" {
  description = "Storage pool for the OVMF EFI disk."
  type        = string
  default     = "local-lvm"
}

variable "include_cdrom_ide2" {
  description = "Whether to declare an empty optical drive at ide2. Legacy VMs all have one (PVE-installer artifact); rebuilt VMs typically don't."
  type        = bool
  default     = true
}
