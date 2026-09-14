#!/usr/bin/env bash
# check-protected-vms.sh — fail if a terraform/prd plan deletes or
# replaces a VM.
#
# Usage: check-protected-vms.sh <plan.json>
#
# <plan.json> is `terraform show -json` of the plan. Exits 0 when the
# plan deletes no VM, 1 when it deletes or replaces one, 2 on usage or
# parse errors.
#
# The second rail: while the managed-vm module's VM resource carries
# prevent_destroy, `terraform plan` refuses such a plan before this
# script runs. It keys on the VM resource type, not on everything under
# module.vm: the module's DNS reservation is deleted once a VM's vms.tf
# entry is gone, and that delete must pass.
#
# Used by the iac-on-push, iac-apply and iac-scheduled-drift Jenkins jobs.

set -euo pipefail

# Exactly one argument: a caller still passing VM names disagrees with
# this script, and must fail rather than have its plan pass unchecked.
if [[ $# -ne 1 ]]; then
    echo "Usage: $(basename "$0") <plan.json>" >&2
    exit 2
fi

plan_json=$1

if [[ ! -f "$plan_json" ]]; then
    echo "$(basename "$0"): $plan_json does not exist" >&2
    exit 2
fi

if ! vms=$(jq -r '
    .resource_changes[]
    | select(.type == "proxmox_virtual_environment_vm")
    | select(any(.change.actions[]; . == "delete"))
    | .address + " (" + (.change.actions | join(",")) + ")"
' "$plan_json"); then
    echo "$(basename "$0"): cannot read $plan_json" >&2
    exit 2
fi

if [[ -n "$vms" ]]; then
    while IFS= read -r vm; do
        echo "check-protected-vms: plan deletes or replaces prd VM $vm" >&2
    done <<<"$vms"
    exit 1
fi
