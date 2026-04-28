module "vm" {
  source = "../../modules/managed-vm"

  name        = "srvk8ss2"
  vm_id       = 107
  pve_node    = "pve2"
  description = "microk8s small worker 2 of 2. OS managed by Ansible (k8s_prd group)."
  tags        = ["ansible-managed", "terraform", "k8s"]

  smbios_uuid = "8d300dfe-abfa-42ab-9d24-47f5bc8944a0"

  # Live VM is still seabios — operator's manual UEFI flip was abandoned.
  # Phase 4's rebuild flips to ovmf (and adds an EFI disk) as part of the
  # rebuild commit. Until then, model matches reality so plan stays clean.
  bios = "seabios"

  cpu_cores   = 3
  cpu_sockets = 1
  memory_mb   = 16384

  managed_disks = [
    { interface = "scsi0", size = 20 },
    { interface = "scsi1", size = 80 },
  ]

  network_devices = [
    { bridge = "vmbr0", mac_address = "BC:24:11:52:D7:84" },
    { bridge = "vmbr0", mac_address = "BC:24:11:09:BF:9D", vlan_id = 2 },
    { bridge = "vmbr1", mac_address = "BC:24:11:53:B9:3F" },
  ]
}
