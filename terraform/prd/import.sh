#!/usr/bin/env bash
# Bulk-import every VM in local.vms (vms.tf) into terraform.tfstate.
#
# Run from terraform/prd/ after `terraform init` and with the Proxmox
# API token available (terraform.tfvars or TF_VAR_proxmox_*).
#
# Idempotent: VMs already present in state are skipped, so re-running on
# a partially-imported tree just fills in the gaps.
#
# When local.vms changes (new VM, renamed VM, moved between PVE nodes),
# update the imports[] list below to match.

set -euo pipefail

cd "$(dirname "$0")"

# Each line: <name> <pve_node> <vmid>
imports=(
  "srvk8ss1 pve1 104"
  "srvk8ss2 pve2 107"
  "srvk8sl1 pve  103"
  "srvceph1 pve1 113"
  "srvceph2 pve2 114"
  "srvceph3 pve  115"
)

for entry in "${imports[@]}"; do
  read -r name node vmid <<<"$entry"
  addr="module.vm[\"${name}\"].proxmox_virtual_environment_vm.this"

  if terraform state show "$addr" >/dev/null 2>&1; then
    printf '[skip]   %s (already in state)\n' "$name"
    continue
  fi

  printf '[import] %s (%s/%s)\n' "$name" "$node" "$vmid"
  terraform import "$addr" "${node}/${vmid}"
done

echo
echo "Done. Run 'terraform plan' to confirm zero diff."
