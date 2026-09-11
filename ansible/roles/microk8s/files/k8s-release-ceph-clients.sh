#!/bin/bash
# Managed by the microk8s Ansible role (microk8s_colocated_ceph). See the
# role README, "Co-located Ceph shutdown".
#
# ExecStop of k8s-release-ceph-clients.service. On a node that runs its own
# microceph, the pods' kernel Ceph clients (krbd devices, kernel CephFS
# mounts) must be gone before microceph stops, or their I/O blocks forever
# and shutdown wedges. Unit ordering runs this after kubelite and containerd
# have stopped and before microceph does. Those stops don't end the pods:
# containerd runs KillMode=process and the pods live in /kubepods, outside
# any unit, so they would survive until the final kill — after Ceph is gone.
# So: end the pods, unmount, unmap.
set -u

KUBEPODS=/sys/fs/cgroup/kubepods
GRACE_SECONDS=20

# Only at shutdown: a manual stop or restart of the unit must not kill every
# pod on the node. --force releases anyway.
if [[ "${1:-}" != "--force" && "$(systemctl is-system-running)" != "stopping" ]]; then
  echo "System is not shutting down; leaving pods and Ceph clients alone (--force overrides)."
  exit 0
fi

populated() { grep -qx 'populated 1' "$KUBEPODS/cgroup.events" 2>/dev/null; }

# Wait up to $1 seconds for /kubepods to empty; fails if it didn't.
wait_empty() {
  local i
  for ((i = 0; i < $1; i++)); do
    populated || return 0
    sleep 1
  done
  ! populated
}

if populated; then
  echo "Stopping pods (SIGTERM, ${GRACE_SECONDS}s grace)"
  find "$KUBEPODS" -name cgroup.procs -exec cat {} + | xargs -r kill -TERM 2>/dev/null
  if ! wait_empty "$GRACE_SECONDS"; then
    echo "Pods still running after ${GRACE_SECONDS}s; killing"
    echo 1 > "$KUBEPODS/cgroup.kill"
    wait_empty 10 || echo "Pods survived SIGKILL"
  fi
fi

# Unmount what kubelet left behind, deepest path first. With the pods gone
# nothing else holds these, so the CephFS sessions and rbd devices free up.
findmnt -rn -o TARGET,FSTYPE,SOURCE |
  awk '$2 == "ceph" || $3 ~ /^\/dev\/rbd/ { print $1 }' |
  sort -r |
  while read -r target; do
    echo "Unmounting $target"
    umount "$target" || echo "Failed to unmount $target"
  done

for dev in /sys/bus/rbd/devices/*; do
  [[ -e "$dev" ]] || continue
  id=${dev##*/}
  echo "Unmapping /dev/rbd$id"
  echo "$id" > /sys/bus/rbd/remove_single_major ||
    echo "$id force" > /sys/bus/rbd/remove_single_major ||
    echo "Failed to unmap /dev/rbd$id"
done

exit 0
