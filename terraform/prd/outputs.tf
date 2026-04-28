output "vm_ids" {
  description = "Map of VM name → VMID."
  value       = { for name, vm in module.vm : name => vm.vm_id }
}

output "nic_macs" {
  description = "Map of VM name → list of NIC MAC addresses (declaration order)."
  value       = { for name, vm in module.vm : name => vm.nic_macs }
}
