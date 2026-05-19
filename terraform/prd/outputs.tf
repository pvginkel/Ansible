output "vm_ids" {
  description = "Map of VM name → VMID."
  value       = { for name, vm in module.vm : name => vm.vm_id }
}

output "nic_macs" {
  description = "Map of VM name → list of NIC MAC addresses (declaration order)."
  value       = { for name, vm in module.vm : name => vm.nic_macs }
}

# Consumed by the bootstrap playbook (rebuild-k8s.yml and, from Phase
# 2, the OpenBao provisioning play): it turns these into a transient
# known_hosts so Ansible's first, pre-certificate connection to a
# freshly-provisioned VM verifies the host without TOFU. Steady-state
# runs never read this — they trust the homelab SSH CA via the
# committed `@cert-authority` line. See AnsibleSpecs slices/ssh-host-ca.md.
output "host_pubkeys" {
  description = "Map of from-scratch VM name → ed25519 host public key (OpenSSH format)."
  value = {
    for name, key in tls_private_key.host_ed25519 :
    name => trimspace(key.public_key_openssh)
  }
}
