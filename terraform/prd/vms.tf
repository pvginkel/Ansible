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

  # Per-VM base config. `network_devices` is deliberately not here — it
  # is the single source of truth in each VM's Ansible host_var
  # (inventories/prd/host_vars/<name>.yml), merged in by the `vms` local
  # below. See AnsibleSpecs slice network-devices-host-vars-sot.
  vms_base = {
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
      memory_mb   = 18 * 1024

      managed_disks = [
        { interface = "scsi0", size = 20 },
        { interface = "scsi1", size = 80 },
      ]

      # NICs: inventories/prd/host_vars/srvk8s2.yml
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
      memory_mb   = 18 * 1024

      managed_disks = [
        { interface = "scsi0", size = 20 },
        { interface = "scsi1", size = 80 },
      ]

      # NICs: inventories/prd/host_vars/srvk8s3.yml
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
      memory_mb   = 6 * 1024

      managed_disks = [
        { interface = "scsi0", size = 60 },
      ]

      # NICs: inventories/prd/host_vars/wrkdevk8s.yml
    }

    srviac = {
      vm_id          = 920
      pve_node       = "pve"
      workload_class = "background"
      from_scratch   = true
      description    = "IaC orchestrator VM — runs Terraform + Ansible against the homelab. Phase 1 (iac-agent). OS managed by Ansible (iac_agent group)."
      tags           = ["ansible-managed", "terraform", "iac"]
      bios           = "ovmf"
      machine        = "q35"

      cpu_cores   = 2
      cpu_sockets = 1
      memory_mb   = 3 * 1024

      managed_disks = [
        { interface = "scsi0", size = 32 },
      ]

      # NICs: inventories/prd/host_vars/srviac.yml
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
      memory_mb   = 18 * 1024

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

      # NICs: inventories/prd/host_vars/srvk8s1.yml
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
      memory_mb   = 10 * 1024

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

      # NICs: inventories/prd/host_vars/srvceph1.yml
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
      memory_mb   = 10 * 1024

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

      # NICs: inventories/prd/host_vars/srvceph2.yml
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
      memory_mb   = 10 * 1024

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

      # NICs: inventories/prd/host_vars/srvceph3.yml
    }

    # OpenBao 3-node Raft cluster (Phase 2). One srvvault per PVE host
    # so a host loss takes one node. Static IPs because srvvaultN are
    # bootstrap-critical and must serve before the dnsmasq pod is
    # reachable (`decisions.md` "Bootstrap-critical hosts do not
    # resolve through the dnsmasq pod"). Raft data and the static seal
    # key both live on the rootfs — no passthrough or data disk.
    srvvault1 = {
      vm_id          = 913
      pve_node       = "pve"
      workload_class = "background"
      from_scratch   = true
      static_ip      = true
      description    = "OpenBao node 1 of 3. OS managed by Ansible (openbao group)."
      tags           = ["ansible-managed", "terraform", "openbao"]
      bios           = "ovmf"
      machine        = "q35"

      cpu_cores   = 2
      cpu_sockets = 1
      memory_mb   = 1024

      managed_disks = [
        { interface = "scsi0", size = 24 },
      ]

      # `pve` declares pve_node_backup_datastore so its VMs land in
      # the cluster vzdump job by default. srvvault1 must opt out:
      # `decisions.md` "OpenBao backup / DR" forbids a PVE backup that
      # would co-locate the static seal key with the Raft data on the
      # same artefact. srvvault2/3 are on pve1/pve2 (no backup
      # datastore) and leave this default.
      exclude_from_backup = true

      # NICs: inventories/prd/host_vars/srvvault1.yml
    }

    srvvault2 = {
      vm_id          = 914
      pve_node       = "pve1"
      workload_class = "background"
      from_scratch   = true
      static_ip      = true
      description    = "OpenBao node 2 of 3. OS managed by Ansible (openbao group)."
      tags           = ["ansible-managed", "terraform", "openbao"]
      bios           = "ovmf"
      machine        = "q35"

      cpu_cores   = 2
      cpu_sockets = 1
      memory_mb   = 1024

      managed_disks = [
        { interface = "scsi0", size = 24 },
      ]

      # NICs: inventories/prd/host_vars/srvvault2.yml
    }

    srvvault3 = {
      vm_id          = 915
      pve_node       = "pve2"
      workload_class = "background"
      from_scratch   = true
      static_ip      = true
      description    = "OpenBao node 3 of 3. OS managed by Ansible (openbao group)."
      tags           = ["ansible-managed", "terraform", "openbao"]
      bios           = "ovmf"
      machine        = "q35"

      cpu_cores   = 2
      cpu_sockets = 1
      memory_mb   = 1024

      managed_disks = [
        { interface = "scsi0", size = 24 },
      ]

      # NICs: inventories/prd/host_vars/srvvault3.yml
    }
  }
}

locals {
  # Merge each VM's network_devices in from its Ansible host_var, so
  # module.vm and the cloud-init render keep seeing
  # each.value.network_devices unchanged. A missing host_var file or a
  # missing network_devices key fails `terraform plan` loudly —
  # intended: a TF-managed VM must have its host_var first.
  vms = {
    for name, vm in local.vms_base :
    name => merge(vm, {
      network_devices = yamldecode(
        file("${path.module}/../../ansible/inventories/prd/host_vars/${name}.yml")
      ).network_devices
    })
  }
}
