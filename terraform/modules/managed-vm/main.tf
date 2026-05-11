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

  # Primary-NIC static addresses plucked from network_devices[0] for the
  # cloud-init `ip_config` block below. PVE renders these into the VM's
  # cloud-init network-config so init-local writes a static netplan at
  # boot 0. Hosts whose primary NIC has no static address in inventory
  # (Ceph today, plus dev-tier dynamic NICs) leave these null and the
  # `ip_config` falls back to "dhcp".
  primary_ipv4_cidr = try(
    [for a in var.network_devices[0].addresses : a if !strcontains(a, ":")][0],
    null
  )
  primary_ipv6_cidr = try(
    [for a in var.network_devices[0].addresses : a if strcontains(a, ":")][0],
    null
  )
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
    floating  = var.memory_mb
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

      # PVE renders cloud-init network-config from this block; cloud-init's
      # init-local stage then writes /etc/netplan/50-cloud-init.yaml from
      # it at boot 0. When network_devices[0] declares a static address,
      # pass that through so eth0 comes up directly on its inventory IP.
      # The block has to be present or PVE writes no network config and
      # the NIC stays admin-down; "dhcp" is the fallback for hosts whose
      # primary NIC has no static address in inventory (Ceph today —
      # dnsmasq reservations supply the address by MAC).
      #
      # The earlier `address = "dhcp"` here caused a race: eth0 would
      # briefly lease a 10.1.1.x address from the dnsmasq sidecar's
      # dynamic pool while kubelite was starting, kube-apiserver
      # auto-detected that IP as its external host, and the lease
      # reconciler wrote it into the `kubernetes` Service Endpoints.
      # user-data's write_files later overwrote netplan with the static
      # IP, but kube-apiserver kept advertising the wrong one until the
      # next restart — leaving KUBE-SVC randomly DNAT'ing 172.17.0.1:443
      # to dead apiserver SEPs.
      ip_config {
        ipv4 {
          address = coalesce(local.primary_ipv4_cidr, "dhcp")
          gateway = var.network_devices[0].gateway
        }
        ipv6 {
          address = coalesce(local.primary_ipv6_cidr, "dhcp")
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
      # Cloud-init user-data is a first-boot artefact — re-rendering
      # its snippet for a running VM accomplishes nothing operational.
      # Without this, bpg's ForceNew on `source_raw.data` of the
      # cloud-init snippet cascades into a VM replace via
      # `initialization.user_data_file_id`, so a one-line edit to the
      # template would rebuild every from-scratch VM. Drift on the
      # written netplan after first boot is Ansible's job (see
      # `static_netplan` in the baseline role). Pick up a template
      # change deliberately via `terraform apply -replace=<vm>`.
      #
      # Narrowed to user_data_file_id specifically (rather than the
      # whole `initialization` block) so that `ip_config` changes do
      # propagate: PVE writes the new ipconfigN to the VM's PENDING
      # config and regenerates the cloud-init drive in place, and the
      # next cold-cycle picks it up. That's the channel for delivering
      # the static-IP fix that closes the early-DHCP race documented
      # on the ip_config block above.
      initialization[0].user_data_file_id,
    ]
  }
}
