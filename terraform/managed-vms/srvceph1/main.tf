module "vm" {
  source = "../../modules/managed-vm"

  name        = "srvceph1"
  vm_id       = 113
  pve_node    = "pve1"
  description = "microceph node 1 of 3. OS managed by Ansible (ceph_prd group)."
  tags        = ["ansible-managed", "terraform", "ceph"]

  smbios_uuid = "4941d812-2dae-4b15-8ba5-9455ca853e52"

  cpu_cores   = 1
  cpu_sockets = 3
  memory_mb   = 10240

  managed_disks = [
    { interface = "scsi0", size = 32 },
    { interface = "scsi1", size = 100 },
  ]

  passthrough_disks = [
    {
      interface         = "scsi2"
      path_in_datastore = "/dev/disk/by-id/ata-Samsung_SSD_870_EVO_2TB_S754NS0X128906Y"
    },
  ]

  network_devices = [
    { bridge = "vmbr0", mac_address = "BC:24:11:A0:CB:D5" },
    { bridge = "vmbr1", mac_address = "BC:24:11:AD:18:01" },
  ]
}
