module "vm" {
  source   = "../modules/managed-vm"
  for_each = local.vms

  name        = each.key
  vm_id       = each.value.vm_id
  pve_node    = each.value.pve_node
  description = each.value.description
  tags        = each.value.tags
  smbios_uuid = each.value.smbios_uuid
  bios        = each.value.bios

  cpu_cores   = each.value.cpu_cores
  cpu_sockets = each.value.cpu_sockets
  memory_mb   = each.value.memory_mb

  managed_disks     = each.value.managed_disks
  passthrough_disks = each.value.passthrough_disks
  network_devices   = each.value.network_devices
}
