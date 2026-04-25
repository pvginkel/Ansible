output "vm_id" {
  description = "Proxmox VMID."
  value       = proxmox_virtual_environment_vm.scratch.vm_id
}

output "vm_name" {
  description = "Hostname assigned via cloud-init."
  value       = proxmox_virtual_environment_vm.scratch.name
}

output "vm_mac" {
  description = "NIC MAC address. dnsmasq pins the IP and DNS reservation off this."
  value       = proxmox_virtual_environment_vm.scratch.network_device[0].mac_address
}

output "ssh_command" {
  description = "Quick one-liner to SSH in as the ansible user once cloud-init has completed. Relies on the operator workstation's `home` search domain to resolve the short name."
  value       = "ssh ansible@${var.vm_name}"
}
