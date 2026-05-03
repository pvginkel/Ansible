locals {
  # From-scratch VMs (i.e. those marked `from_scratch = true` in
  # vms.tf) build off a downloaded Ubuntu cloud image, render their
  # own cloud-init snippet, and ship a per-VM SSH host keypair into
  # tfstate. Adopted VMs (everything else, today: cephs and the
  # not-yet-rebuilt k8s nodes) are imported into state and need none
  # of this scaffolding. The split is per-VM, not per-environment —
  # phase 4b rebuilds the k8s nodes one at a time, each flipping
  # individually from adopted to from-scratch.
  vms_from_scratch = { for name, vm in local.vms : name => vm if try(vm.from_scratch, false) }

  # The same ansible.pub the bootstrap role drops on every managed
  # host. cloud-init authorises it on the ansible user so the role
  # apply that follows the rebuild can connect.
  ansible_ssh_public_key = trimspace(file("${path.module}/../../ansible/roles/bootstrap/files/ansible.pub"))

  # Ubuntu cloud image must land on each pve_node that hosts at least
  # one from-scratch VM. Adopted-only nodes need no download.
  from_scratch_pve_nodes = toset([for vm in local.vms_from_scratch : vm.pve_node])
}

resource "proxmox_download_file" "ubuntu_cloud_image" {
  for_each     = local.from_scratch_pve_nodes
  content_type = "iso"
  datastore_id = "local"
  node_name    = each.value
  url          = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
  file_name    = "noble-server-cloudimg-amd64.img"
  overwrite    = false
}

# Per-VM SSH host key. Generated once, persists in tfstate, embedded
# into cloud-init so the VM boots with a deterministic identity, and
# exported into ansible/files/known_hosts.d/prd. A rebuild without
# `terraform taint` keeps the same key so Ansible never has to TOFU.
resource "tls_private_key" "host_ed25519" {
  for_each  = local.vms_from_scratch
  algorithm = "ED25519"
}

resource "local_file" "known_hosts_prd" {
  # Only materialize the file once at least one VM is from-scratch.
  # Until then the resource is absent so plan shows zero diff after
  # the scaffold commit and before the first per-VM rebuild commit.
  for_each        = length(local.vms_from_scratch) > 0 ? toset(["prd"]) : toset([])
  filename        = "${path.module}/../../ansible/files/known_hosts.d/prd"
  file_permission = "0644"
  content = join("", [
    for name in sort(keys(local.vms_from_scratch)) :
    "${name},${name}.home ${trimspace(tls_private_key.host_ed25519[name].public_key_openssh)}\n"
  ])
}

resource "proxmox_virtual_environment_file" "cloud_init" {
  for_each     = local.vms_from_scratch
  content_type = "snippets"
  datastore_id = "local"
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

module "vm" {
  source   = "../modules/managed-vm"
  for_each = local.vms

  name        = each.key
  vm_id       = each.value.vm_id
  pve_node    = each.value.pve_node
  description = each.value.description
  tags        = each.value.tags
  smbios_uuid = try(each.value.smbios_uuid, null)
  bios        = each.value.bios
  machine     = try(each.value.machine, null)

  cpu_cores    = each.value.cpu_cores
  cpu_sockets  = each.value.cpu_sockets
  cpu_affinity = each.value.pve_node == "pve" ? local.workload_affinity_cores[each.value.workload_class] : null
  memory_mb    = each.value.memory_mb

  managed_disks     = each.value.managed_disks
  passthrough_disks = try(each.value.passthrough_disks, [])
  network_devices   = each.value.network_devices

  # Adopted VMs were created with a PVE-installer ide2 cdrom; from-
  # scratch builds boot off the cloud image and don't need it.
  include_cdrom_ide2 = !try(each.value.from_scratch, false)

  cloud_init = try(each.value.from_scratch, false) ? {
    image_file_id     = proxmox_download_file.ubuntu_cloud_image[each.value.pve_node].id
    user_data_file_id = proxmox_virtual_environment_file.cloud_init[each.key].id
  } : null
}
