locals {
  ansible_ssh_public_key = trimspace(coalesce(
    var.ansible_ssh_public_key,
    file("${path.module}/../../ansible/roles/bootstrap/files/ansible.pub"),
  ))

  # Deterministic MAC (locally-administered prefix 02:A7:F3, then VMID
  # big-endian over two bytes, then NIC index). Fixing the MAC lets dnsmasq
  # carry the IP reservation by MAC without a separate allocation registry.
  vm_macs = {
    for name, vm in local.vms :
    name => format("02:A7:F3:%02X:%02X:00",
      floor(vm.vm_id / 256),
      vm.vm_id % 256,
    )
  }
}

# Per-VM SSH host key. Generated once, persisted in tfstate, embedded into
# cloud-init so the VM boots with a deterministic identity, and exported
# into ansible/files/known_hosts.d/scratch (one combined file for the
# inventory) so Ansible can verify each host on first contact without TOFU.
resource "tls_private_key" "host_ed25519" {
  for_each  = local.vms
  algorithm = "ED25519"
}

resource "local_file" "known_hosts_entries" {
  filename        = "${path.module}/../../ansible/files/known_hosts.d/scratch"
  file_permission = "0644"
  content = join("", [
    for name, _ in local.vms :
    "${name},${name}.home ${trimspace(tls_private_key.host_ed25519[name].public_key_openssh)}\n"
  ])
}

resource "proxmox_download_file" "ubuntu_cloud_image" {
  content_type = "iso"
  datastore_id = var.image_datastore
  node_name    = var.pve_node
  url          = var.ubuntu_cloud_image_url
  file_name    = "noble-server-cloudimg-amd64.img"
  overwrite    = false
}

resource "proxmox_virtual_environment_file" "cloud_init" {
  for_each     = local.vms
  content_type = "snippets"
  datastore_id = var.image_datastore
  node_name    = each.value.pve_node

  source_raw {
    data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
      vm_name                = each.key
      ansible_ssh_public_key = local.ansible_ssh_public_key
      # Indented so the template's `|` block reads it as a single literal scalar.
      host_ed25519_private = indent(6, tls_private_key.host_ed25519[each.key].private_key_openssh)
      host_ed25519_public  = trimspace(tls_private_key.host_ed25519[each.key].public_key_openssh)
    })
    file_name = "${each.key}-user-data.yaml"
  }
}

resource "proxmox_virtual_environment_vm" "scratch" {
  for_each = local.vms

  name        = each.key
  node_name   = each.value.pve_node
  vm_id       = each.value.vm_id
  description = each.value.description
  tags        = each.value.tags

  bios    = "ovmf"
  machine = "q35"

  agent {
    enabled = true
    type    = "virtio"
  }

  cpu {
    cores    = each.value.cpu_cores
    type     = "host"
    affinity = each.value.pve_node == "pve" ? local.workload_affinity_cores[each.value.workload_class] : null
  }

  memory {
    dedicated = each.value.memory_mb
    floating  = each.value.memory_mb
  }

  operating_system {
    type = "l26"
  }

  scsi_hardware = "virtio-scsi-single"

  efi_disk {
    datastore_id      = var.vm_storage
    type              = "4m"
    pre_enrolled_keys = true
  }

  disk {
    datastore_id = var.vm_storage
    file_id      = proxmox_download_file.ubuntu_cloud_image.id
    interface    = "scsi0"
    iothread     = true
    discard      = "on"
    ssd          = true
    # Scratch VMs sit on `pve`, the only node with a backup datastore today;
    # the cluster vzdump job still picks them up. Disposable doesn't mean
    # "exclude from backup" — keeps the rule "everything on pve gets backed
    # up" intact and the per-disk flag uniform across the inventory.
    backup = true
    size   = each.value.disk_size_gb
  }

  network_device {
    bridge      = var.vm_bridge
    model       = "virtio"
    mac_address = local.vm_macs[each.key]
    firewall    = true
  }

  # Ubuntu cloud images expect ttyS0 for cloud-init output and emergency console.
  serial_device {}

  initialization {
    datastore_id = var.vm_storage

    # DHCP on both stacks. No address / gateway / dns here — dnsmasq is the
    # authority for IPv4 (reservation by MAC) and the router handles IPv6
    # (SLAAC / DHCPv6). The block has to exist or Proxmox writes no network
    # config and the NIC stays admin-down.
    ip_config {
      ipv4 {
        address = "dhcp"
      }
      ipv6 {
        address = "dhcp"
      }
    }

    user_data_file_id = proxmox_virtual_environment_file.cloud_init[each.key].id
  }

  lifecycle {
    ignore_changes = [
      # The Ubuntu cloud-image release behind `current/` rolls forward; ignore
      # drift on the image file to avoid terraform wanting to rebuild the VM
      # every time Canonical publishes a new point release. Recreate the VM
      # deliberately (terraform taint / replace) to pick up a newer image.
      disk[0].file_id,
    ]
  }
}
