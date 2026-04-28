output "vms" {
  description = "Per-VM identity: VMID, hostname, NIC MAC. dnsmasq pins the IP and DNS reservation off the MAC."
  value = {
    for name, vm in proxmox_virtual_environment_vm.scratch :
    name => {
      vm_id = vm.vm_id
      name  = vm.name
      mac   = vm.network_device[0].mac_address
    }
  }
}

output "ssh_commands" {
  description = "Quick one-liners to SSH in as the ansible user once cloud-init has completed."
  value = {
    for name, _ in local.vms : name => "ssh ansible@${name}"
  }
}
