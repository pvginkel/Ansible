#!/usr/bin/env bash
# check-terraform-drift.sh — fail if a terraform/prd plan changes
# anything but the cloud-init snippet files.
#
# Usage: check-terraform-drift.sh <plan.json>
#
# <plan.json> is `terraform show -json` of a plan with changes. Exits 0
# when its changes are all to proxmox_virtual_environment_file.cloud_init,
# 1 when anything else changes, 2 on usage or parse errors.
#
# The snippets are first-boot artefacts: the managed-vm module ignores
# initialization[0].user_data_file_id, so no running VM reads one, and
# an apply that builds a VM renders its snippet first. Every rotation of
# ansible/roles/bootstrap/files/ansible.pub re-renders all of them.
#
# Used by the IaC/Scheduled Drift Jenkins job.

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $(basename "$0") <plan.json>" >&2
    exit 2
fi

plan_json=$1

if [[ ! -f "$plan_json" ]]; then
    echo "$(basename "$0"): $plan_json does not exist" >&2
    exit 2
fi

# One line per change: "snippet <address>" or "other <address> (<actions>)".
if ! changes=$(jq -r '
    ((.resource_changes // [])[]
        | select(.change.actions != ["no-op"])
        | if .mode == "managed" and .type == "proxmox_virtual_environment_file"
                and .name == "cloud_init" and .module_address == null
            then "snippet " + .address
            else "other " + .address + " (" + (.change.actions | join(",")) + ")"
            end),
    ((.output_changes // {}) | to_entries[]
        | select(.value.actions != ["no-op"])
        | "other output." + .key + " (" + (.value.actions | join(",")) + ")")
' "$plan_json"); then
    echo "$(basename "$0"): cannot read $plan_json" >&2
    exit 2
fi

snippets=$(sed -n 's/^snippet //p' <<<"$changes")
others=$(sed -n 's/^other //p' <<<"$changes")

if [[ -z "$others" && -n "$snippets" ]]; then
    echo "check-terraform-drift: not drift — the plan only re-renders cloud-init snippets, which no running VM reads:"
    sed 's/^/  /' <<<"$snippets"
    exit 0
fi

echo "DRIFT: terraform plan proposes changes against prd" >&2
if [[ -n "$others" ]]; then
    while IFS= read -r change; do
        echo "check-terraform-drift: plan changes $change" >&2
    done <<<"$others"
fi
exit 1
