#!/usr/bin/env bash
# dqlite-watch-probe.sh — detect a frozen apiserver watch cache on THIS node.
#
# Background: on microk8s 1.34–1.35 the k8s-dqlite watch poll loop can die
# on a transient (network blip) and stay dead until k8s-dqlite is restarted
# (upstream k8s-dqlite#364 / microk8s#5386, fix PR #365 still unmerged). A
# dead watch freezes this apiserver's watch cache at an old resourceVersion:
# controllers list/watch from the frozen cache and stop reconciling, while
# quorum reads stay current. See docs/runbooks/dqlite-watch-freeze.md.
#
# Detection compares, on the node's OWN apiserver, a cache-served read
# (?resourceVersion=0, served from the watch cache) against a quorum read
# (no resourceVersion, read through to the datastore) of a constantly-
# renewing object — the kube-controller-manager lease. A node is judged
# FROZEN only on the unambiguous signature: the cache rv stays pinned across
# every sample while the quorum rv advances and the gap exceeds a floor.
# Mere lag (cache advancing but behind) is NOT a freeze and is left alone.
#
# Exit codes:
#   0  healthy or inconclusive — do nothing
#   3  frozen — caller should restart k8s-dqlite on this node
#   2  apiserver unreachable — a different, bigger fault; caller must NOT
#      restart dqlite blind, surface for a human instead
#
# Args: <samples> <interval_seconds> <min_lag>

set -uo pipefail

SAMPLES="${1:-4}"
INTERVAL="${2:-4}"
MIN_LAG="${3:-500}"
LEASE="/apis/coordination.k8s.io/v1/namespaces/kube-system/leases/kube-controller-manager"

rv() {  # $1 = query suffix, e.g. "?resourceVersion=0" for the cache-served read
  microk8s kubectl get --raw "${LEASE}${1}" 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["metadata"]["resourceVersion"])' 2>/dev/null
}

first_cache=""
last_cache=""
first_fresh=""
last_fresh=""
cache_pinned=1

for i in $(seq 1 "$SAMPLES"); do
  cache="$(rv '?resourceVersion=0')"
  fresh="$(rv '')"
  if [[ -z "$cache" || -z "$fresh" ]]; then
    echo "probe: apiserver unreachable on $(hostname) (sample $i/$SAMPLES)" >&2
    exit 2
  fi
  echo "probe: sample $i/$SAMPLES cache_rv=$cache fresh_rv=$fresh lag=$((fresh - cache))"
  [[ -z "$first_cache" ]] && first_cache="$cache" && first_fresh="$fresh"
  [[ -n "$last_cache" && "$cache" != "$last_cache" ]] && cache_pinned=0
  last_cache="$cache"
  last_fresh="$fresh"
  [[ "$i" -lt "$SAMPLES" ]] && sleep "$INTERVAL"
done

fresh_advanced=0
[[ "$last_fresh" -gt "$first_fresh" ]] && fresh_advanced=1
lag=$((last_fresh - last_cache))

if [[ "$cache_pinned" -eq 1 && "$fresh_advanced" -eq 1 && "$lag" -ge "$MIN_LAG" ]]; then
  echo "FROZEN: watch cache pinned at rv=$last_cache while the datastore advanced to rv=$last_fresh (lag=$lag) on $(hostname)" >&2
  exit 3
fi

echo "probe: healthy on $(hostname) (cache_pinned=$cache_pinned fresh_advanced=$fresh_advanced lag=$lag)"
exit 0
