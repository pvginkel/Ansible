module "vm" {
  source = "../../modules/managed-vm"

  name        = "srvceph2"
  vm_id       = 114
  pve_node    = "pve2"
  description = "microceph node 2 of 3. OS managed by Ansible (ceph_prd group)."
  tags        = ["ansible-managed", "terraform", "ceph"]

  smbios_uuid = "8fa7841f-a1d5-4347-85f7-c3c725e3e721"

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
      path_in_datastore = "/dev/disk/by-id/ata-Samsung_SSD_870_EVO_2TB_S754NS0X128908E"
    },
  ]

  network_devices = [
    { bridge = "vmbr0", mac_address = "BC:24:11:7B:37:DC" },
    { bridge = "vmbr1", mac_address = "BC:24:11:94:B2:99" },
  ]
}
