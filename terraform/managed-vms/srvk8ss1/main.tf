module "vm" {
  source = "../../modules/managed-vm"

  name        = "srvk8ss1"
  vm_id       = 104
  pve_node    = "pve1"
  description = "microk8s small worker 1 of 2. OS managed by Ansible (k8s_prd group)."
  tags        = ["ansible-managed", "terraform", "k8s"]

  smbios_uuid = "6ea14220-9148-4be3-aaf2-4b68cdacf52d"

  cpu_cores   = 3
  cpu_sockets = 1
  memory_mb   = 16384

  managed_disks = [
    { interface = "scsi0", size = 20 },
    { interface = "scsi1", size = 80 },
  ]

  network_devices = [
    { bridge = "vmbr0", mac_address = "BC:24:11:2B:B5:73" },
    { bridge = "vmbr0", mac_address = "BC:24:11:89:6C:E2", vlan_id = 2 },
    { bridge = "vmbr1", mac_address = "BC:24:11:30:0B:F3" },
  ]
}
