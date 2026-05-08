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
  # carries intentional_spare_disks).
  pve_host_vars_path  = "${path.module}/../../../ansible/inventories/prd/host_vars/${var.pve_node}.yml"
  pve_node_has_backup = try(yamldecode(file(local.pve_host_vars_path)).pve_node_backup_datastore, null) != null
}

# dnsmasq reservation registered with the sidecar API. Lands before the
# VM (depends_on below) so the first DHCP request hits a known reservation.
# network_devices[0] is the vmbr0 reservation NIC by convention; tag-2 and
# vmbr1 NICs carry static addresses declared per-VM in vms.tf, no IPAM.
#
# Skipped when var.static_ip = true (Ceph nodes today): those hosts are
# bring-up-tier infrastructure with hardcoded IPs in HelmCharts'
# static-hosts.yaml. See decisions.md "Ceph IPs are static infrastructure".
resource "homelab_dns_reservation" "this" {
  count    = var.static_ip ? 0 : 1
  hostname = var.name
  mac      = var.network_devices[0].mac_address

  lifecycle {
    precondition {
      condition     = var.network_devices[0].bridge == "vmbr0" && var.network_devices[0].vlan_id == 0
      error_message = "network_devices[0] must be on vmbr0 with vlan_id=0 — that NIC is the dnsmasq-reservation NIC."
    }
  }
}

resource "proxmox_virtual_environment_vm" "this" {
  depends_on = [homelab_dns_reservation.this]

  name        = var.name
  node_name   = var.pve_node
  vm_id       = var.vm_id
  description = var.description
  tags        = var.tags

  bios       = var.bios
  machine    = var.machine
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
    cores    = var.cpu_cores
    sockets  = var.cpu_sockets
    type     = var.cpu_type
    affinity = var.cpu_affinity
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
      # First managed disk holds the cloud image when from-scratch.
      # Otherwise null = unset → bpg leaves state as-is (adopted disks
      # imported without a file_id stay that way).
      file_id = (var.cloud_init != null && disk.key == 0) ? var.cloud_init.image_file_id : null
      ssd     = (var.cloud_init != null && disk.key == 0) ? true : null
      backup  = local.pve_node_has_backup
    }
  }

  dynamic "disk" {
    for_each = var.passthrough_disks
    content {
      datastore_id      = ""
      path_in_datastore = disk.value.path_in_datastore
      interface         = disk.value.interface
      backup            = false
    }
  }

  dynamic "serial_device" {
    # Cloud-init logging + emergency console on ttyS0. Required only on
    # from-scratch builds; adopted VMs without it carry on as-is.
    for_each = var.cloud_init != null ? [1] : []
    content {}
  }

  dynamic "initialization" {
    for_each = var.cloud_init != null ? [var.cloud_init] : []
    content {
      datastore_id = initialization.value.datastore_id

      # DHCP only — dnsmasq is the IPv4 reservation authority (keyed on
      # the deterministic MAC); the router handles IPv6 (SLAAC/DHCPv6).
      # The block has to be present or PVE writes no network config and
      # the NIC stays admin-down.
      ip_config {
        ipv4 {
          address = "dhcp"
        }
        ipv6 {
          address = "dhcp"
        }
      }

      user_data_file_id = initialization.value.user_data_file_id
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
      # Cloud image rolls forward under `current/`; ignore so a newer
      # Canonical point release doesn't make every plan want to rebuild
      # the VM. Pick up a new image deliberately via `terraform apply
      # -replace`. No-op on adopted VMs (no file_id on disk[0]).
      disk[0].file_id,
      # Cloud-init is a first-boot artefact — re-rendering its snippet
      # for a running VM accomplishes nothing operational. Without this,
      # bpg's ForceNew on `source_raw.data` of the cloud-init snippet
      # cascades into a VM replace via `initialization.user_data_file_id`,
      # so a one-line edit to the template would rebuild every from-
      # scratch VM. Drift on these fields after first boot is Ansible's
      # job (see `static_netplan` in the baseline role). Pick up a
      # template change deliberately via `terraform apply -replace=<vm>`.
      initialization,
    ]
  }
}
