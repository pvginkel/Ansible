#!/usr/bin/env bash
# Behaviour harness for playbooks/rotate-ansible-key.yml. Runs the
# playbook against an inventory of local stand-ins: two hosts reached
# over SSH, a VM reached through a fake qm on a fake PVE node, and a VM
# that does not exist. A fake ssh (bin/ssh) takes the proof's login when
# its key is in the host's authorized_keys file; the VMs are reached
# over SSH only by that login, any other connection to one fails the run.
# Checks each run's PLAY RECAP and the files it leaves. Run in the iac
# sidecar, where poetry lives:
#
#   cexec iac ./playbooks/tests/rotate-ansible-key/run.sh   (from ansible/)
#
# `kc project test --project ansible` runs it. A real sshd's verdict,
# sudo on a PVE node and the guest agent itself stay the live run's.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
book=$(cd "$here/../.." && pwd)/rotate-ansible-key.yml
work=$(mktemp -d)
banner_pid=

cleanup() {
  [ -z "$banner_pid" ] || kill "$banner_pid" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

fail() {
  echo "rotate-ansible-key harness: $1" >&2
  [ -n "${2-}" ] && tail -n 60 "$2" >&2
  exit 1
}

mkdir -p "$work/keys" "$work/log" "$work/tmp" "$work/known" \
  "$work/hosts/harnesshost1" "$work/hosts/harnesshost2" \
  "$work/guests/901/home/ansible/.ssh" "$work/guests/901/etc/ssh"
for key in old new other third wrong; do
  ssh-keygen -q -t ed25519 -N '' -C ansible -f "$work/keys/$key"
done
ssh-keygen -q -t ed25519 -N '' -C root@harnessvm -f "$work/keys/hostkey"
cp "$work/keys/hostkey.pub" "$work/guests/901/etc/ssh/ssh_host_ed25519_key.pub"

# Every host starts with the old key and one it must never touch; the
# VM's file lacks its final newline.
vm_keys=$work/guests/901/home/ansible/.ssh/authorized_keys
for host in harnesshost1 harnesshost2; do
  cat "$work/keys/other.pub" "$work/keys/old.pub" >"$work/hosts/$host/authorized_keys"
  ln -s "$work/hosts/$host/authorized_keys" "$work/known/$host"
done
cat "$work/keys/other.pub" "$work/keys/old.pub" | head -c -1 >"$vm_keys"
chmod 0600 "$work"/hosts/*/authorized_keys "$vm_keys"
ln -s "$vm_keys" "$work/known/harnessvm"
ln -s "$work/guests/901/etc/ssh/ssh_host_ed25519_key.pub" "$work/known/harnessvm.hostkey"

# Stands in for the VM's sshd until the proof's wait: a banner, no more.
python3 - "$work/port" <<'EOF' &
import socket, sys
server = socket.create_server(("127.0.0.1", 0))
with open(sys.argv[1] + ".new", "w") as f:
    f.write(str(server.getsockname()[1]))
import os
os.rename(sys.argv[1] + ".new", sys.argv[1])
while True:
    conn, _ = server.accept()
    conn.sendall(b"SSH-2.0-OpenSSH_harness\r\n")
    conn.close()
EOF
banner_pid=$!
for ((i = 0; i < 50; i++)); do
  [ -f "$work/port" ] && break
  sleep 0.1
done
[ -f "$work/port" ] || fail "the banner server did not start"

cat >"$work/inventory.yml" <<EOF
all:
  vars:
    ansible_connection: local
    ansible_become: false
    ansible_python_interpreter: "{{ ansible_playbook_python }}"
  children:
    proxmox:
      hosts:
        harnesspve:
    ansible_key:
      vars:
        key_rotation_user: $(id -un)
      children:
        ansible_key_always_up:
          hosts:
            harnesshost1:
              key_rotation_authorized_keys: $work/hosts/harnesshost1/authorized_keys
            harnesshost2:
              key_rotation_authorized_keys: $work/hosts/harnesshost2/authorized_keys
        ansible_key_may_be_off:
          vars:
            ansible_connection: ssh
            key_rotation_authorized_keys: /home/ansible/.ssh/authorized_keys
          hosts:
            harnessvm:
              ansible_host: 127.0.0.1
              ansible_port: $(cat "$work/port")
            harnessgone:
EOF

export ANSIBLE_CONFIG=$here/ansible.cfg PATH=$here/bin:$PATH TMPDIR=$work/tmp \
  HARNESS_GUESTS=$work/guests HARNESS_KEYS=$work/known HARNESS_LOG=$work/log
run=0

# vars STATE PUBLIC [PRIVATE [VM_VARS]] — an extra-vars file; VM_VARS is
# JSON, by default where both VMs are.
vars() {
  local file=$work/vars-$run.json
  python3 - "$file" "$work/keys" "$@" <<'EOF'
import json, sys
file, keys, state, public = sys.argv[1:5]
private = sys.argv[5] if len(sys.argv) > 5 else ""
vms = sys.argv[6] if len(sys.argv) > 6 else '{"harnessvm": "harnesspve/901", "harnessgone": "absent"}'
values = {"key_rotation_state": state, "key_rotation_public": open(f"{keys}/{public}.pub").read()}
if private:
    values["key_rotation_private"] = open(f"{keys}/{private}").read()
values.update({f"key_rotation_vm_{vm}": where for vm, where in json.loads(vms).items()})
with open(file, "w") as f:
    json.dump(values, f)
EOF
  echo "$file"
}

# converge LABEL EXPECT EXTRA_VARS [ansible-playbook args…] — EXPECT is
# failed=0; changed=0 for that and nothing changed; changed for that and
# something changed; or fails, for a run that must fail.
converge() {
  local label=$1 expect=$2 extra=$3 log recap code=0
  shift 3
  run=$((run + 1))
  log=$work/run-$run.log
  poetry run ansible-playbook -i "$work/inventory.yml" "$book" -e "@$extra" "$@" \
    </dev/null >"$log" 2>&1 || code=$?
  recap=$(sed -n '/^PLAY RECAP/,$p' "$log" | grep -E '^[a-z0-9]+ +: ok=') || fail "$label: no PLAY RECAP:" "$log"
  if grep -q 'PRIVATE KEY' "$log"; then fail "$label: a private half is in the output:" "$log"; fi
  if [ "$expect" = fails ]; then
    [ "$code" -ne 0 ] || fail "$label: expected the run to fail:" "$log"
  else
    [ "$code" -eq 0 ] || fail "$label: ansible-playbook failed:" "$log"
    if grep -vqE ' failed=0 ' <<<"$recap" || grep -vqE ' unreachable=0 ' <<<"$recap"; then
      fail "$label: expected failed=0:" "$log"
    fi
    case $expect in
      changed=0) if grep -vq ' changed=0 ' <<<"$recap"; then fail "$label: expected changed=0:" "$log"; fi ;;
      changed) grep -vq ' changed=0 ' <<<"$recap" || fail "$label: expected a change:" "$log" ;;
    esac
  fi
  [ -z "$(ls -A "$work/tmp")" ] || fail "$label: the proof's directory is left behind:" "$log"
  echo "ok $run - $label"
}

# holds KEY... — every host's authorized_keys holds exactly these keys,
# one per line.
holds() {
  local want file
  want=$(for key in "$@"; do cut -d' ' -f2 "$work/keys/$key.pub"; done | sort)
  for file in "$work"/hosts/*/authorized_keys "$vm_keys"; do
    [ "$(cut -d' ' -f2 "$file" | sort)" = "$want" ] || fail "$file holds other keys than $*:" "$file"
  done
}

converge "add the new key" changed "$(vars present new new)"
holds other old new
[ "$(wc -l <"$vm_keys")" -eq 3 ] || fail "the VM's file is not three lines:" "$vm_keys"
for host in harnesshost1 harnesshost2 harnessvm; do
  grep -q "$host" "$work/log/ssh.log" || fail "the add run did not log in to $host:" "$work/log/ssh.log"
done
if grep -q harnessgone "$work/log/ssh.log"; then fail "a VM that does not exist was logged in to"; fi
while read -r identity; do
  [ ! -e "$identity" ] || fail "the proof's private half $identity is left behind"
done <"$work/log/identities"
converge "add it again" changed=0 "$(vars present new new)"
converge "--check the add" changed=0 "$(vars present new new)" --check

before=$(md5sum "$work"/hosts/*/authorized_keys "$vm_keys")
converge "--check the removal of the old key" changed "$(vars absent old)" --check
[ "$(md5sum "$work"/hosts/*/authorized_keys "$vm_keys")" = "$before" ] || fail "--check changed a file"
converge "remove the old key" changed "$(vars absent old)"
holds other new
converge "remove it again" changed=0 "$(vars absent old)"

converge "a VM it is told nothing about" fails "$(vars present third third '{"harnessvm": "harnesspve/901"}')"
grep -q 'key_rotation_vm_harnessgone must be' "$work/run-$run.log" || fail "not refused for harnessgone:" "$work/run-$run.log"
holds other new
converge "a VM on a node outside proxmox" fails \
  "$(vars present third third '{"harnessvm": "pve9/901", "harnessgone": "absent"}')"
grep -q 'key_rotation_vm_harnessvm must be' "$work/run-$run.log" || fail "not refused for harnessvm:" "$work/run-$run.log"
holds other new
converge "a proof the hosts refuse" fails "$(vars present third wrong)"
grep -c 'Permission denied' "$work/run-$run.log" | grep -qx 3 || fail "not every host refused the proof:" "$work/run-$run.log"
converge "remove the refused key" changed "$(vars absent third)"
holds other new
# The guest script fails inside the VM while qm itself exits 0.
mv "$vm_keys" "$vm_keys.away"
converge "a VM whose authorized_keys is missing" fails "$(vars absent old)"
grep -q 'authorized_keys does not exist' "$work/run-$run.log" || fail "not failed on the VM's file:" "$work/run-$run.log"
mv "$vm_keys.away" "$vm_keys"
holds other new

for key in old new third; do
  if grep -qF "$(cut -d' ' -f2 "$work/keys/$key.pub")" "$work/log/qm.log"; then
    fail "a public half is on qm's command line:" "$work/log/qm.log"
  fi
done
if grep -q 'PRIVATE KEY' "$work/log/ssh.log" "$work/log/qm.log"; then fail "a private half is on a command line"; fi
if grep -v '"901"' "$work/log/qm.log"; then fail "qm was called for another VM than 901"; fi
echo "rotate-ansible-key harness: $run runs as expected"
