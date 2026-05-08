locals {
  # Workload-class → CPU core range for VMs hosted on `pve`. Single source
  # of truth; the module call site below resolves each VM's affinity from
  # this map keyed on its `workload_class`. `pve1`/`pve2` are not
  # core-zoned, so VMs there pass `cpu_affinity = null`. See decisions.md
  # "Proxmox VM CPU affinity".
  workload_affinity_cores = {
    interactive = "0-11"
    background  = "12-19"
  }

  vms = {
    srvk8s2 = {
      vm_id          = 911
      pve_node       = "pve1"
      workload_class = "background"
      from_scratch   = true
      static_ip      = true
      description    = "microk8s node 2 of 3. OS managed by Ansible (k8s_prd group)."
      tags           = ["ansible-managed", "terraform", "k8s"]
      bios           = "ovmf"
      machine        = "q35"

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
        {
          bridge      = "vmbr0"
          mac_address = "02:A7:F3:03:8F:00"
          addresses   = ["10.1.0.28/16", "2a10:3781:565a:1::28/64"]
          gateway     = "10.1.0.1"
          accept_ra   = false
          nameservers = ["8.8.8.8", "8.8.4.4"]
          search      = ["home"]
        },
        {
          bridge      = "vmbr0"
          mac_address = "02:A7:F3:03:8F:01"
          vlan_id     = 2
          addresses   = ["10.2.0.28/16", "2a10:3781:565a::28/64"]
        },
        {
          bridge      = "vmbr1"
          mac_address = "02:A7:F3:03:8F:02"
          addresses   = ["192.168.188.28/24", "fdd0:6a51:35de::28/64"]
        },
      ]
    }

    srvk8s3 = {
      vm_id          = 912
      pve_node       = "pve2"
      workload_class = "background"
      from_scratch   = true
      static_ip      = true
      description    = "microk8s node 3 of 3. OS managed by Ansible (k8s_prd group)."
      tags           = ["ansible-managed", "terraform", "k8s"]
      bios           = "ovmf"
      machine        = "q35"

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
        {
          bridge      = "vmbr0"
          mac_address = "02:A7:F3:03:90:00"
          addresses   = ["10.1.0.29/16", "2a10:3781:565a:1::29/64"]
          gateway     = "10.1.0.1"
          accept_ra   = false
          nameservers = ["8.8.8.8", "8.8.4.4"]
          search      = ["home"]
        },
        {
          bridge      = "vmbr0"
          mac_address = "02:A7:F3:03:90:01"
          vlan_id     = 2
          addresses   = ["10.2.0.29/16", "2a10:3781:565a::29/64"]
        },
        {
          bridge      = "vmbr1"
          mac_address = "02:A7:F3:03:90:02"
          addresses   = ["192.168.188.29/24", "fdd0:6a51:35de::29/64"]
        },
      ]
    }

    wrkdevk8s = {
      vm_id          = 919
      pve_node       = "pve"
      workload_class = "background"
      from_scratch   = true
      description    = "microk8s dev single-node cluster (k8s_dev group). HelmCharts iteration target."
      tags           = ["ansible-managed", "terraform", "k8s"]
      bios           = "ovmf"
      machine        = "q35"

      cpu_cores   = 4
      cpu_sockets = 1
      memory_mb   = 6144

      managed_disks = [
        { interface = "scsi0", size = 60 },
      ]

      # vmbr0 is dev-tier dynamic — DHCP via the per-VM `homelab_dns_reservation`.
      # wrkdevk8s doesn't host registry/dnsmasq pods (dev pulls from external
      # `registry-dev`), so the bring-up cycle that pins prd k8s + Ceph to
      # static IPs on vmbr0 doesn't apply — see decisions.md "Ceph nodes and
      # prd k8s nodes are static infrastructure".
      # vmbr1 is the 10 Gb backplane, hand-curated static address — wrkdevk8s
      # reaches non-cluster services on that subnet. The cloud-init template
      # renders a netplan stanza for any NIC with `addresses` set, regardless
      # of `static_ip`, so the hybrid (dynamic vmbr0, static vmbr1) is fine.
      # Deterministic MACs: VMID 919 = 0x0397.
      network_devices = [
        {
          bridge      = "vmbr0"
          mac_address = "02:A7:F3:03:97:00"
        },
        {
          bridge      = "vmbr1"
          mac_address = "02:A7:F3:03:97:01"
          addresses   = ["192.168.188.17/24", "fdd0:6a51:35de::17/64"]
        },
      ]
    }

    srvk8s1 = {
      vm_id          = 910
      pve_node       = "pve"
      workload_class = "background"
      from_scratch   = true
      static_ip      = true
      description    = "microk8s node 1 of 3 (carries zpool2 via NVMe passthrough). OS managed by Ansible (k8s_prd group)."
      tags           = ["ansible-managed", "terraform", "k8s"]
      bios           = "ovmf"
      machine        = "q35"

      cpu_cores   = 8
      cpu_sockets = 1
      memory_mb   = 14336

      managed_disks = [
        { interface = "scsi0", size = 20 },
        { interface = "scsi1", size = 80 },
      ]

      # NVMe backs zpool2 (decisions.md "k8s node capability labels");
      # imported into the rebuilt VM at scsi2 in the same TF apply, then
      # `zpool import zpool2` runs from `rebuild-k8s.yml`.
      passthrough_disks = [
        { interface = "scsi2", path_in_datastore = "/dev/disk/by-id/nvme-Samsung_SSD_980_500GB_S64DNX0RC21332X" },
      ]

      # Three NICs: house net, k8s workload VLAN (vmbr0 tag=2), 10 Gb
      # backplane. Deterministic MAC: 02:A7:F3:VV:VV:EE where VV:VV is
      # VMID big-endian and EE is the NIC index. VMID 910 = 0x038E.
      network_devices = [
        {
          bridge      = "vmbr0"
          mac_address = "02:A7:F3:03:8E:00"
          addresses   = ["10.1.0.27/16", "2a10:3781:565a:1::27/64"]
          gateway     = "10.1.0.1"
          accept_ra   = false
          nameservers = ["8.8.8.8", "8.8.4.4"]
          search      = ["home"]
        },
        {
          bridge      = "vmbr0"
          mac_address = "02:A7:F3:03:8E:01"
          vlan_id     = 2
          addresses   = ["10.2.0.27/16", "2a10:3781:565a::27/64"]
        },
        {
          bridge      = "vmbr1"
          mac_address = "02:A7:F3:03:8E:02"
          addresses   = ["192.168.188.27/24", "fdd0:6a51:35de::27/64"]
        },
      ]
    }

    srvceph1 = {
      vm_id          = 113
      pve_node       = "pve1"
      workload_class = "background"
      description    = "microceph node 1 of 3. OS managed by Ansible (ceph_prd group)."
      tags           = ["ansible-managed", "terraform", "ceph"]
      smbios_uuid    = "4941d812-2dae-4b15-8ba5-9455ca853e52"
      bios           = "ovmf"
      static_ip      = true

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
      vm_id          = 114
      pve_node       = "pve2"
      workload_class = "background"
      description    = "microceph node 2 of 3. OS managed by Ansible (ceph_prd group)."
      tags           = ["ansible-managed", "terraform", "ceph"]
      smbios_uuid    = "8fa7841f-a1d5-4347-85f7-c3c725e3e721"
      bios           = "ovmf"
      static_ip      = true

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
      vm_id          = 115
      pve_node       = "pve"
      workload_class = "background"
      description    = "microceph node 3 of 3. OS managed by Ansible (ceph_prd group)."
      tags           = ["ansible-managed", "terraform", "ceph"]
      smbios_uuid    = "bc789ae7-120d-4722-b854-56202ff2fb74"
      bios           = "ovmf"
      static_ip      = true

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
