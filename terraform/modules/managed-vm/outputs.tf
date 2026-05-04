output "vm_id" {
  description = "Proxmox VMID."
  value       = proxmox_virtual_environment_vm.this.vm_id
}

output "vm_name" {
  description = "VM hostname."
  value       = proxmox_virtual_environment_vm.this.name
}

output "nic_macs" {
  description = "MAC addresses for each NIC, in declaration order. dnsmasq pins IP and DNS off these."
  value       = [for nic in proxmox_virtual_environment_vm.this.network_device : nic.mac_address]
}

output "dns_ipv4" {
  description = "IPv4 the sidecar API allocated for this VM's vmbr0 reservation. Null for static_ip VMs (no reservation; address is in HelmCharts' static-hosts.yaml)."
  value       = one(homelab_dns_reservation.this[*].ipv4)
}
