locals {
  vms = {
    srvk8s2 = {
      vm_id        = 911
      pve_node     = "pve1"
      from_scratch = true
      description  = "microk8s node 2 of 3. OS managed by Ansible (k8s_prd group)."
      tags         = ["ansible-managed", "terraform", "k8s"]
      bios         = "ovmf"
      machine      = "q35"

      cpu_cores   = 3
      cpu_sockets = 1
      memory_mb   = 16384

      managed_disks = [
        { interface = "scsi0", size = 20 },
        { interface = "scsi1", size = 80 },
      ]

      # Three NICs: house net, k8s workload VLAN (vmbr0 tag=2), 10 Gb
      # backplane. Deterministic MAC: VMID 911 = 0x038F.
      network_devices = [
        { bridge = "vmbr0", mac_address = "02:A7:F3:03:8F:00" },
        { bridge = "vmbr0", mac_address = "02:A7:F3:03:8F:01", vlan_id = 2 },
        { bridge = "vmbr1", mac_address = "02:A7:F3:03:8F:02" },
      ]
    }

    srvk8s3 = {
      vm_id        = 912
      pve_node     = "pve2"
      from_scratch = true
      description  = "microk8s node 3 of 3. OS managed by Ansible (k8s_prd group)."
      tags         = ["ansible-managed", "terraform", "k8s"]
      # Live VM is seabios; rebuild flips to ovmf (the operator's
      # earlier manual flip was abandoned). The from-scratch path
      # adds the EFI disk implicitly via the module's efi_disk block.
      bios    = "ovmf"
      machine = "q35"

      cpu_cores   = 3
      cpu_sockets = 1
      memory_mb   = 16384

      managed_disks = [
        { interface = "scsi0", size = 20 },
        { interface = "scsi1", size = 80 },
      ]

      # Three NICs: house net, k8s workload VLAN (vmbr0 tag=2), 10 Gb
      # backplane. Deterministic MAC: VMID 912 = 0x0390.
      network_devices = [
        { bridge = "vmbr0", mac_address = "02:A7:F3:03:90:00" },
        { bridge = "vmbr0", mac_address = "02:A7:F3:03:90:01", vlan_id = 2 },
        { bridge = "vmbr1", mac_address = "02:A7:F3:03:90:02" },
      ]
    }

    wrkdevk8s = {
      vm_id        = 919
      pve_node     = "pve"
      from_scratch = true
      description  = "microk8s dev single-node cluster (k8s_dev group). HelmCharts iteration target."
      tags         = ["ansible-managed", "terraform", "k8s"]
      bios         = "ovmf"
      machine      = "q35"

      cpu_cores   = 4
      cpu_sockets = 1
      memory_mb   = 6144

      # Single 60 GB root disk. wrkdevk8s does not use a separate
      # data disk (live shape preserved) — dev cluster, simpler is
      # fine. snap state under /var/snap/microk8s + a small image
      # cache fit comfortably; no zpool, no Ceph, no MetalLB pool of
      # any size that needs its own volume.
      managed_disks = [
        { interface = "scsi0", size = 60 },
      ]

      # Two NICs: house net + 10 Gb backplane. wrkdevk8s does not
      # join the prd k8s workload VLAN (vmbr0 tag=2) — that segment
      # is reserved for prd cluster services.
      # Deterministic MAC: VMID 919 = 0x0397.
      network_devices = [
        { bridge = "vmbr0", mac_address = "02:A7:F3:03:97:00" },
        { bridge = "vmbr1", mac_address = "02:A7:F3:03:97:01" },
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
