module "vm" {
  source = "../../modules/managed-vm"

  name        = "srvk8sl1"
  vm_id       = 103
  description = "microk8s control-plane / large worker. OS managed by Ansible (k8s_prd group)."
  tags        = ["ansible-managed", "terraform", "k8s"]

  smbios_uuid = "db20f84e-5549-4717-92c2-90e6d8957b3c"

  cpu_cores   = 8
  cpu_sockets = 1
  memory_mb   = 14336

  managed_disks = [
    { interface = "scsi0", size = 20 },
    { interface = "scsi1", size = 80 },
  ]

  # nvme1n1 — cloud-sync ZFS volume passthrough.
  passthrough_disks = [
    {
      interface         = "scsi2"
      path_in_datastore = "/dev/disk/by-id/nvme-Samsung_SSD_980_500GB_S64DNX0RC21332X"
    },
  ]

  # Three NICs: house net, k8s workload VLAN (vmbr0 tag=2), 10 Gb backplane.
  network_devices = [
    { bridge = "vmbr0", mac_address = "BC:24:11:3D:56:09" },
    { bridge = "vmbr0", mac_address = "BC:24:11:3F:F2:71", vlan_id = 2 },
    { bridge = "vmbr1", mac_address = "BC:24:11:C2:03:95" },
  ]
}
