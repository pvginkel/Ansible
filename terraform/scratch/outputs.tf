output "vm_id" {
  description = "Proxmox VMID."
  value       = proxmox_virtual_environment_vm.scratch.vm_id
}

output "vm_name" {
  description = "Hostname assigned via cloud-init."
  value       = proxmox_virtual_environment_vm.scratch.name
}

output "vm_ipv4_address" {
  description = "Static IPv4 assigned via cloud-init (CIDR notation)."
  value       = var.vm_ipv4_address
}

output "vm_fqdn" {
  description = "FQDN. Matches the DNS entry that must resolve to vm_ipv4_address."
  value       = "${var.vm_name}.${var.vm_dns_domain}"
}

output "ssh_command" {
  description = "Quick one-liner to SSH in as the ansible user once cloud-init has completed."
  value       = "ssh ansible@${var.vm_name}.${var.vm_dns_domain}"
}
