#!/usr/bin/env bash
# check-ansible-drift.sh — run ansible-playbook --check --diff and exit
# non-zero if the play reports any pending changes.
#
# Usage: check-ansible-drift.sh <playbook> [ansible-playbook args ...]
#
# Exits:
#   0  the play is a clean no-op
#   1  drift detected (changed > 0 in the recap)
#   N  ansible-playbook itself failed with exit code N
#
# Used by the iac-scheduled-drift Jenkins job.

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $(basename "$0") <playbook> [args ...]" >&2
    exit 2
fi

# Stream stdout/stderr to the console live (so the Jenkins job log
# updates in real time, not all at once at the end) while teeing to a
# temp file the recap grep then reads. PIPESTATUS[0] preserves
# ansible-playbook's exit code through the pipe; without it `$?` would
# reflect tee's exit code.
log=$(mktemp)
trap 'rm -f "$log"' EXIT

set +e
ansible-playbook --check --diff "$@" 2>&1 | tee "$log"
rc=${PIPESTATUS[0]}
set -e

if [[ $rc -ne 0 ]]; then
    echo "$(basename "$0"): ansible-playbook --check failed (rc=$rc)" >&2
    exit "$rc"
fi

changed=$(grep -oE 'changed=[0-9]+' "$log" | awk -F= '{sum+=$2} END {print sum+0}')

if [[ "$changed" -gt 0 ]]; then
    echo "$(basename "$0"): DRIFT — ansible reports $changed pending changes" >&2
    exit 1
fi
