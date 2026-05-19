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

# Seeds the transient known_hosts the bootstrap playbook builds for
# Ansible's first, pre-certificate connection to a freshly-provisioned
# scratch VM. Steady-state runs trust the homelab SSH CA via the
# committed `@cert-authority` line. See AnsibleSpecs slices/ssh-host-ca.md.
output "host_pubkeys" {
  description = "Map of scratch VM name → ed25519 host public key (OpenSSH format)."
  value = {
    for name, key in tls_private_key.host_ed25519 :
    name => trimspace(key.public_key_openssh)
  }
}
