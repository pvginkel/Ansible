#!/usr/bin/env bash
# Behaviour harness for the openbao role's tasks/approle.yml. Starts a
# throwaway dev OpenBao, converges play.yml against it, and checks each
# run's PLAY RECAP. Run in the iac sidecar, where bao and poetry live:
#
#   cexec iac ./roles/openbao/tests/approle/run.sh   (from ansible/)
#
# `kc project test --project ansible` runs it. Only approle.yml is
# covered: a dev server stands in for the API it talks to, not for the
# srvvault hosts the rest of the role configures.
#
# The sequence starts from an applied server, then deletes the rotator
# AppRole so --check meets an AppRole it would create. That is slice
# 045's regression: --check failed with HTTP 404 reading the role_id
# of an AppRole not yet created.
#
# The server listens on a free port, so harnesses running side by side
# in one sidecar never share one; it does not store its root token in
# ~/.vault-token, which is the shared home; and it is stopped by pid,
# under a timeout that ends it if the stop is missed.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
# Seconds the dev server gets to answer before the harness gives up.
start_wait=${HARNESS_START_WAIT:-30}

work=$(mktemp -d)
port=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
export BAO_ADDR=http://127.0.0.1:$port BAO_TOKEN=root

timeout 600 bao server -dev -dev-root-token-id=root -dev-no-store-token \
  -dev-listen-address="127.0.0.1:$port" >"$work/bao.log" 2>&1 &
pid=$!

cleanup() {
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

fail() {
  echo "approle harness: $1" >&2
  [ -n "${2-}" ] && tail -n 40 "$2" >&2
  exit 1
}

for ((i = 0; ; i++)); do
  kill -0 "$pid" 2>/dev/null || fail "the dev server exited before it answered:" "$work/bao.log"
  bao status >/dev/null 2>&1 && break
  [ "$i" -lt "$start_wait" ] || fail "the dev server did not answer on $BAO_ADDR within ${start_wait}s:" "$work/bao.log"
  sleep 1
done

export ANSIBLE_CONFIG=$here/ansible.cfg
cd "$here"
run=0

# converge LABEL EXPECT [ansible-playbook args…] — EXPECT is `failed=0`,
# or `changed=0` for that and nothing changed.
converge() {
  local label=$1 expect=$2 log recap
  shift 2
  run=$((run + 1))
  log=$work/run-$run.log
  poetry run ansible-playbook -i localhost, play.yml \
    -e harness_bao_addr="$BAO_ADDR" -e harness_staging_dir="$work/staging" \
    "$@" >"$log" 2>&1 || fail "$label: ansible-playbook failed:" "$log"
  recap=$(grep -E '^localhost +:' "$log") || fail "$label: no PLAY RECAP:" "$log"
  [[ $recap == *" failed=0 "* ]] || fail "$label: expected failed=0:" "$log"
  if [ "$expect" = changed=0 ]; then
    [[ $recap == *" changed=0 "* ]] || fail "$label: expected changed=0:" "$log"
  fi
  echo "ok $run - $label: $(tr -s ' ' <<<"$recap")"
}

converge "apply to a fresh server" failed=0
converge "apply again" changed=0
converge "--check after apply" changed=0 --check
bao delete auth/approle/role/rotator >/dev/null
converge "--check with AppRole rotator deleted" failed=0 --check
converge "apply, recreating rotator" failed=0
converge "--check after the recreate" changed=0 --check
echo "approle harness: $run runs as expected"
