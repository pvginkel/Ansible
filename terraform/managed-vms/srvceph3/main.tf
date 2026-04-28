module "vm" {
  source = "../../modules/managed-vm"

  name        = "srvceph3"
  vm_id       = 115
  description = "microceph node 3 of 3. OS managed by Ansible (ceph_prd group)."
  tags        = ["ansible-managed", "terraform", "ceph"]

  smbios_uuid = "bc789ae7-120d-4722-b854-56202ff2fb74"

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
      path_in_datastore = "/dev/disk/by-id/ata-Samsung_SSD_870_EVO_2TB_S754NS0X128911L"
    },
  ]

  network_devices = [
    { bridge = "vmbr0", mac_address = "BC:24:11:53:D3:C8" },
    { bridge = "vmbr1", mac_address = "BC:24:11:43:5A:8A" },
  ]
}
