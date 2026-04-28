locals {
  # Single source of truth for the per-disk `backup=` flag: a managed disk
  # gets backup=true iff its PVE node declares pve_node_backup_datastore in
  # Ansible inventory. Passthrough disks are always backup=false.
  #
  # A missing host_vars file means "no overrides," same as Ansible — pve1
  # and pve2 carry no node-specific attributes today, so their files don't
  # exist. Treat that case as an empty dict, not an error.
  pve_host_vars_path  = "${path.module}/../../../ansible/inventories/prd/host_vars/${var.pve_node}.yml"
  pve_host_vars       = fileexists(local.pve_host_vars_path) ? yamldecode(file(local.pve_host_vars_path)) : {}
  pve_node_has_backup = try(local.pve_host_vars.pve_node_backup_datastore, null) != null
}

resource "proxmox_virtual_environment_vm" "this" {
  name        = var.name
  node_name   = var.pve_node
  vm_id       = var.vm_id
  description = var.description
  tags        = var.tags

  bios       = var.bios
  on_boot    = true
  boot_order = var.boot_order

  # Cluster-member-aware: TF writes config but does not reboot. Reboots are
  # operator-driven through Ansible's drain-aware update playbook. See
  # decisions.md "Terraform applies on cluster members never reboot directly".
  reboot_after_update = false

  agent {
    enabled = true
  }

  cpu {
    cores   = var.cpu_cores
    sockets = var.cpu_sockets
    type    = var.cpu_type
  }

  memory {
    dedicated = var.memory_mb
  }

  operating_system {
    type = "l26"
  }

  scsi_hardware = "virtio-scsi-single"

  dynamic "smbios" {
    for_each = var.smbios_uuid == null ? [] : [var.smbios_uuid]
    content {
      uuid = smbios.value
    }
  }

  dynamic "efi_disk" {
    for_each = var.bios == "ovmf" ? [1] : []
    content {
      datastore_id      = var.efi_disk_datastore
      type              = "4m"
      pre_enrolled_keys = true
    }
  }

  dynamic "cdrom" {
    for_each = var.include_cdrom_ide2 ? [1] : []
    content {
      interface = "ide2"
      file_id   = "none"
    }
  }

  dynamic "disk" {
    for_each = var.managed_disks
    content {
      datastore_id = disk.value.datastore_id
      interface    = disk.value.interface
      size         = disk.value.size
      discard      = disk.value.discard
      iothread     = disk.value.iothread
      backup       = local.pve_node_has_backup
    }
  }

  # Passthrough disks must follow managed disks in declaration order so
  # state's disk[] indexing matches what bpg's import set.
  # TF can import these but cannot create/modify them via API token (PVE
  # restriction on arbitrary filesystem paths). On rebuild, the passthrough
  # block is removed from the module before TF runs, then Ansible reattaches.
  dynamic "disk" {
    for_each = var.passthrough_disks
    content {
      datastore_id      = ""
      path_in_datastore = disk.value.path_in_datastore
      interface         = disk.value.interface
      backup            = false
    }
  }

  dynamic "network_device" {
    for_each = var.network_devices
    content {
      bridge      = network_device.value.bridge
      mac_address = network_device.value.mac_address
      vlan_id     = network_device.value.vlan_id
      firewall    = network_device.value.firewall
      model       = network_device.value.model
    }
  }

  lifecycle {
    ignore_changes = [
      # Reconciled by Ansible (proxmox_host role on `pve`), not Terraform.
      # See decisions.md "Proxmox VM CPU affinity".
      cpu[0].affinity,
    ]
  }
}
