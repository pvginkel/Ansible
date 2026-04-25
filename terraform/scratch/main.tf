locals {
  ansible_ssh_public_key = trimspace(coalesce(
    var.ansible_ssh_public_key,
    file("${path.module}/../../ansible/roles/bootstrap/files/ansible.pub"),
  ))

  cloud_init_user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    vm_name                = var.vm_name
    vm_dns_domain          = var.vm_dns_domain
    ansible_ssh_public_key = local.ansible_ssh_public_key
  })

  # Deterministic MAC (locally-administered prefix 02:A7:F3, then VMID big-endian
  # over two bytes, then NIC index). Fixing the MAC lets dnsmasq carry the IP
  # reservation by MAC without a separate allocation registry.
  vm_mac = format("02:A7:F3:%02X:%02X:00",
    floor(var.vm_id / 256),
    var.vm_id % 256,
  )
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
  content_type = "snippets"
  datastore_id = var.image_datastore
  node_name    = var.pve_node

  source_raw {
    data      = local.cloud_init_user_data
    file_name = "${var.vm_name}-user-data.yaml"
  }
}

resource "proxmox_virtual_environment_vm" "scratch" {
  name        = var.vm_name
  node_name   = var.pve_node
  vm_id       = var.vm_id
  description = "Phase 1 scratch VM — disposable; managed by terraform/scratch."
  tags        = ["scratch", "ansible-managed", "terraform"]

  agent {
    enabled = true
  }

  cpu {
    cores = var.vm_cpu_cores
    type  = "host"
  }

  memory {
    dedicated = var.vm_memory_mb
  }

  operating_system {
    type = "l26"
  }

  scsi_hardware = "virtio-scsi-single"

  disk {
    datastore_id = var.vm_storage
    file_id      = proxmox_download_file.ubuntu_cloud_image.id
    interface    = "scsi0"
    iothread     = true
    discard      = "on"
    ssd          = true
    size         = var.vm_disk_size_gb
  }

  network_device {
    bridge      = var.vm_bridge
    model       = "virtio"
    mac_address = local.vm_mac
  }

  # Ubuntu cloud images expect ttyS0 for cloud-init output and emergency console.
  serial_device {}

  initialization {
    datastore_id = var.vm_storage

    ip_config {
      ipv4 {
        address = var.vm_ipv4_address
        gateway = var.vm_ipv4_gateway
      }
      ipv6 {
        address = "dhcp"
      }
    }

    dns {
      servers = var.vm_dns_servers
      domain  = var.vm_dns_domain
    }

    user_data_file_id = proxmox_virtual_environment_file.cloud_init.id
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
