#!/usr/bin/env bash
# check-protected-vms.sh — fail if a terraform plan proposes destroy
# or replace on any of the protected VMs named in the args.
#
# Usage: check-protected-vms.sh <plan.json> <vm1> [vm2 ...]
#
# Exits 0 when the plan is safe, 1 when any protected VM is slated for
# destroy/replace, 2 on usage/parse errors.
#
# Used by the iac-on-push and iac-scheduled-drift Jenkins jobs.

set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "Usage: $(basename "$0") <plan.json> <vm-name> [vm-name ...]" >&2
    exit 2
fi

plan_json=$1
shift

if [[ ! -f "$plan_json" ]]; then
    echo "$(basename "$0"): $plan_json does not exist" >&2
    exit 2
fi

# Build a jq alternation pattern for the VM names.
vm_alt=$(printf "%s|" "$@" | sed 's/|$//')

jq_filter='
    .resource_changes[] | select(
        (.change.actions == ["delete"] or .change.actions == ["create", "delete"])
        and (.address | test("module\\.vm\\[\"(" + $vms + ")\"\\]"))
    )
'

if jq -e --arg vms "$vm_alt" "$jq_filter" "$plan_json" >/dev/null; then
    echo "check-protected-vms: plan proposes destroy/replace on a protected VM:" >&2
    jq -r --arg vms "$vm_alt" "$jq_filter | \"  - \" + .address + \" (\" + (.change.actions | join(\",\")) + \")\"" "$plan_json" >&2
    exit 1
fi
