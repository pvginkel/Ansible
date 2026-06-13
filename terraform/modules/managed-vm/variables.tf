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

variable "cpu_affinity" {
  description = "PVE core range to pin this VM to (e.g. \"0-11\", \"12-19\"). Null leaves the VM unpinned. See decisions.md \"Proxmox VM CPU affinity\"."
  type        = string
  default     = null
}

variable "memory_mb" {
  description = "Dedicated memory in MiB."
  type        = number
}

variable "managed_disks" {
  description = "Disks backed by a PVE storage pool (root, data). Backup flag is computed from `pve_node`'s host_vars. `ssd` defaults true (all VM-backing storage here is SSD) so the guest reports rotational=0 — matters for guests that tune on it (e.g. a co-located BlueStore OSD)."
  type = list(object({
    interface    = string
    size         = number
    datastore_id = optional(string, "local-lvm")
    discard      = optional(string, "on")
    iothread     = optional(bool, true)
    ssd          = optional(bool, true)
  }))
  default = []
}

variable "passthrough_disks" {
  description = "Block devices passed through from the PVE host (Ceph OSDs, ZFS volumes). Always backup=false. See decisions.md \"Disk passthrough on managed VMs\". `ssd` defaults true (all VM-backing storage here is SSD); it makes the guest report rotational=0, which BlueStore reads to pick its SSD I/O path. `cache` defaults to `none` — correct for a guest that owns caching/durability itself (ZFS ARC, BlueStore). Replicated Ceph OSDs opt into `writeback` per-disk to let host RAM absorb commits; ZFS vdevs must NOT (double-caching + no redundancy to cover the volatile window)."
  type = list(object({
    interface         = string
    path_in_datastore = string
    ssd               = optional(bool, true)
    cache             = optional(string, "none")
    iothread          = optional(bool, true)
    discard           = optional(string, "on")
  }))
  default = []
}

variable "network_devices" {
  description = "VM NICs. Order matters — bpg state indexes by position, not by interface. The static-IP fields (addresses/gateway/accept_ra/nameservers/search) are consumed by the cloud-init template at the prd/dev level when var.static_ip = true; the bpg resource itself only reads bridge/mac_address/vlan_id/firewall/model."
  type = list(object({
    bridge      = string
    mac_address = string
    vlan_id     = optional(number, 0)
    firewall    = optional(bool, true)
    model       = optional(string, "virtio")
    addresses   = optional(list(string), [])
    gateway     = optional(string, null)
    accept_ra   = optional(bool, true)
    nameservers = optional(list(string), [])
    search      = optional(list(string), [])
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

variable "machine" {
  description = "QEMU machine type. \"q35\" pairs with OVMF for from-scratch cloud-init builds; null leaves it for PVE to pick (the path adopted VMs took at original create time)."
  type        = string
  default     = null
}

variable "cloud_init" {
  description = "Cloud-init wiring — when non-null, the module builds the from-scratch shape: scsi0 boots from `image_file_id`, an `initialization` block points at `user_data_file_id`, and a serial console is added. Omit (null) for the adoption shape, where the VM is imported into state from a pre-existing PVE entity. See decisions.md \"Adoption is a waypoint; rebuild is the parity event\"."
  type = object({
    image_file_id     = string
    user_data_file_id = string
    datastore_id      = optional(string, "local-lvm")
  })
  default = null
}

variable "static_ip" {
  description = "Skip the homelab_dns_reservation resource for this VM. Bring-up-tier hosts (Ceph) carry hardcoded IPs in HelmCharts' static-hosts.yaml and must not flow through the dynamic reservation API — the registry container depends on Ceph storage to boot, and any chain that puts Ceph addressing behind the API creates a cold-boot ordering failure. See decisions.md."
  type        = bool
  default     = false
}

variable "exclude_from_backup" {
  description = "Force backup=false on managed disks regardless of whether pve_node declares a backup datastore. For VMs whose disk must never be captured by the cluster vzdump job — e.g. srvvault1, where a PVE backup would co-locate the OpenBao seal key with the Raft data. See decisions.md \"OpenBao backup / DR\"."
  type        = bool
  default     = false
}
