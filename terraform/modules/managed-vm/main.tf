locals {
  # Single source of truth for the per-disk `backup=` flag: a managed disk
  # gets backup=true iff its PVE node declares pve_node_backup_datastore in
  # Ansible inventory. Passthrough disks are always backup=false.
  #
  # try() collapses two "no override" cases into the same false result:
  # the host_vars file doesn't exist (pve1/pve2 today carry no per-node
  # attributes), or the file exists but doesn't set this key. A
  # fileexists()/yamldecode() conditional fails type unification once
  # other modules add unrelated keys to a host_vars file (e.g. pve.yml
  # carries intentional_spare_disks, proxmox_workload_affinity_cores).
  pve_host_vars_path  = "${path.module}/../../../ansible/inventories/prd/host_vars/${var.pve_node}.yml"
  pve_node_has_backup = try(yamldecode(file(local.pve_host_vars_path)).pve_node_backup_datastore, null) != null
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
      # Slots 2 and 3 are reserved for passthrough disks owned by the
      # `proxmox_host` role. PVE rejects API-token writes to passthrough
      # blocks, so TF cannot mutate them — ignoring the slots prevents
      # plan churn after Ansible attaches them. Today every managed VM
      # has 2 managed disks (scsi0=root, scsi1=data); a future VM with
      # 3+ managed disks needs to revisit this convention. Adopted ceph
      # VMs still declare passthroughs in their TF entries (until they
      # rebuild in Phase 5); the ignore is a no-op there because state
      # and config match from import.
      disk[2],
      disk[3],
    ]
  }
}
