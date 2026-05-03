locals {
  # Scratch VMs used to exercise the microk8s role (Phase 4). Both run on
  # `pve` so they share one cloud-image download. VMID range 900-909 is
  # reserved for scratch; 901/902 leave 900 free for any future single-VM
  # scratch use without collision.

  # Workload-class → CPU core range for VMs hosted on `pve`. Mirrored
  # from terraform/prd/vms.tf; both configs run against the same physical
  # host so the zoning is identical. See decisions.md "Proxmox VM CPU
  # affinity".
  workload_affinity_cores = {
    interactive = "0-11"
    background  = "12-19"
  }

  vms = {
    wrkscratchk8s1 = {
      vm_id          = 901
      pve_node       = "pve"
      workload_class = "background"
      description    = "Phase 4 scratch microk8s node 1 of 2 — disposable; exercises the microk8s role."
      tags           = ["scratch", "ansible-managed", "terraform", "k8s"]
      cpu_cores      = 2
      memory_mb      = 4096
      disk_size_gb   = 20
    }
    wrkscratchk8s2 = {
      vm_id          = 902
      pve_node       = "pve"
      workload_class = "background"
      description    = "Phase 4 scratch microk8s node 2 of 2 — disposable; exercises the microk8s role's join path."
      tags           = ["scratch", "ansible-managed", "terraform", "k8s"]
      cpu_cores      = 2
      memory_mb      = 4096
      disk_size_gb   = 20
    }
  }
}
