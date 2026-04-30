locals {
  vms = {
    srvk8ss1 = {
      vm_id       = 104
      pve_node    = "pve1"
      description = "microk8s small worker 1 of 2. OS managed by Ansible (k8s_prd group)."
      tags        = ["ansible-managed", "terraform", "k8s"]
      smbios_uuid = "6ea14220-9148-4be3-aaf2-4b68cdacf52d"
      bios        = "ovmf"

      cpu_cores   = 3
      cpu_sockets = 1
      memory_mb   = 16384

      managed_disks = [
        { interface = "scsi0", size = 20 },
        { interface = "scsi1", size = 80 },
      ]
      passthrough_disks = []

      network_devices = [
        { bridge = "vmbr0", mac_address = "BC:24:11:2B:B5:73" },
        { bridge = "vmbr0", mac_address = "BC:24:11:89:6C:E2", vlan_id = 2 },
        { bridge = "vmbr1", mac_address = "BC:24:11:30:0B:F3" },
      ]
    }

    srvk8ss2 = {
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
      passthrough_disks = []

      network_devices = [
        { bridge = "vmbr0", mac_address = "BC:24:11:52:D7:84" },
        { bridge = "vmbr0", mac_address = "BC:24:11:09:BF:9D", vlan_id = 2 },
        { bridge = "vmbr1", mac_address = "BC:24:11:53:B9:3F" },
      ]
    }

    srvk8s1 = {
      vm_id        = 910
      pve_node     = "pve"
      from_scratch = true
      description  = "microk8s node 1 of 3 (carries zpool2 via NVMe passthrough). OS managed by Ansible (k8s_prd group)."
      tags         = ["ansible-managed", "terraform", "k8s"]
      bios         = "ovmf"
      machine      = "q35"

      cpu_cores   = 8
      cpu_sockets = 1
      memory_mb   = 14336

      managed_disks = [
        { interface = "scsi0", size = 20 },
        { interface = "scsi1", size = 80 },
      ]

      # NVMe at nvme-Samsung_SSD_980_500GB_S64DNX0RC21332X backs zpool2
      # (decisions.md "k8s node capability labels"). Owned by Ansible's
      # proxmox_host role; declared in host_vars/srvk8s1.yml at rebuild
      # time and attached via `qm set` post-VM-create.

      # Three NICs: house net, k8s workload VLAN (vmbr0 tag=2), 10 Gb
      # backplane. Deterministic MAC: 02:A7:F3:VV:VV:EE where VV:VV is
      # VMID big-endian and EE is the NIC index. VMID 910 = 0x038E.
      network_devices = [
        { bridge = "vmbr0", mac_address = "02:A7:F3:03:8E:00" },
        { bridge = "vmbr0", mac_address = "02:A7:F3:03:8E:01", vlan_id = 2 },
        { bridge = "vmbr1", mac_address = "02:A7:F3:03:8E:02" },
      ]
    }

    srvceph1 = {
      vm_id       = 113
      pve_node    = "pve1"
      description = "microceph node 1 of 3. OS managed by Ansible (ceph_prd group)."
      tags        = ["ansible-managed", "terraform", "ceph"]
      smbios_uuid = "4941d812-2dae-4b15-8ba5-9455ca853e52"
      bios        = "ovmf"

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

    srvceph2 = {
      vm_id       = 114
      pve_node    = "pve2"
      description = "microceph node 2 of 3. OS managed by Ansible (ceph_prd group)."
      tags        = ["ansible-managed", "terraform", "ceph"]
      smbios_uuid = "8fa7841f-a1d5-4347-85f7-c3c725e3e721"
      bios        = "ovmf"

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

    srvceph3 = {
      vm_id       = 115
      pve_node    = "pve"
      description = "microceph node 3 of 3. OS managed by Ansible (ceph_prd group)."
      tags        = ["ansible-managed", "terraform", "ceph"]
      smbios_uuid = "bc789ae7-120d-4722-b854-56202ff2fb74"
      bios        = "ovmf"

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
  }
}
