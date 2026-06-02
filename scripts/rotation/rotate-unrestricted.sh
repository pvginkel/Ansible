#!/usr/bin/env bash
#
# rotate-unrestricted.sh (scripts/rotation/) — auto-rotate `rotation=unrestricted`
# leaves (mechanism `random`): stateless app secrets we own outright. For
# each, regenerate every key with a fresh random value of the same length,
# write it, and stamp `rotated_at` (preserving rotation/mechanism/notes).
#
# This is the `random` handler. Other mechanisms (keycloak, postgres, …)
# need their own handlers with system access — do NOT extend this one to
# them blindly: db/rabbitmq carry a derived `url` key and need an engine
# ALTER, and several coordinated leaves are whole-file blobs.
#
# Dry-run by default (lists what it would rotate). Pass --apply to write.
# Consumers pick up the new value on their next ESO refresh + pod restart;
# the touched secret names are printed so you can `kubectl rollout restart`.
#
# Requires a logged-in bao session with write access + BAO_CACERT, jq.
# NOTE: this reads the current values (to match length) and writes new
# ones — run it yourself; it never prints a secret value.
#
set -uo pipefail
MOUNT="${OPENBAO_KV_MOUNT:-kv}"
APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1
command -v bao >/dev/null || { echo "bao not found" >&2; exit 1; }
command -v jq  >/dev/null || { echo "jq not found"  >&2; exit 1; }

walk() {
  local p="$1" keys k
  if [ -z "$p" ]; then keys=$(bao kv list -format=json -mount="$MOUNT" 2>/dev/null | jq -r '.[]?')
  else keys=$(bao kv list -format=json -mount="$MOUNT" "$p" 2>/dev/null | jq -r '.[]?'); fi
  for k in $keys; do case "$k" in */) walk "$p$k";; *) echo "$p$k";; esac; done
}
rand() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"; }

work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
n=0
while IFS= read -r leaf; do
  [ -n "$leaf" ] || continue
  cm=$(bao kv metadata get -format=json -mount="$MOUNT" "$leaf" 2>/dev/null | jq -c '.data.custom_metadata // {}')
  [ "$(printf '%s' "$cm" | jq -r '.rotation // empty')" = unrestricted ] || continue
  mech=$(printf '%s' "$cm" | jq -r '.rotation_mechanism // empty')
  if [ "$mech" != random ]; then echo "skip $leaf — mechanism=$mech is not handled here" >&2; continue; fi
  n=$((n + 1))
  if [ "$APPLY" -eq 0 ]; then echo "would rotate kv/$leaf"; continue; fi

  data=$(bao kv get -format=json -mount="$MOUNT" "$leaf" | jq '.data.data')
  newjson='{}'
  for k in $(printf '%s' "$data" | jq -r 'keys[]'); do
    len=$(printf '%s' "$data" | jq -r --arg k "$k" '.[$k] | tostring | length')
    [ "$len" -ge 16 ] 2>/dev/null || len=48
    newjson=$(printf '%s' "$newjson" | jq --arg k "$k" --arg v "$(rand "$len")" '.[$k]=$v')
  done
  printf '%s' "$newjson" > "$work/leaf.json"
  bao kv put -mount="$MOUNT" "$leaf" @"$work/leaf.json" >/dev/null
  shred -u "$work/leaf.json"

  notes=$(printf '%s' "$cm" | jq -r '.notes // empty')
  args=(-mount="$MOUNT" -custom-metadata=rotation=unrestricted -custom-metadata=rotation_mechanism=random -custom-metadata=rotated_at="$(date -I)")
  [ -n "$notes" ] && args+=(-custom-metadata=notes="$notes")
  bao kv metadata put "${args[@]}" "$leaf" >/dev/null
  echo "rotated kv/$leaf"
done < <(walk "" | sort)

if [ "$APPLY" -eq 0 ]; then
  echo "(dry run — $n leaves would rotate; re-run with --apply)" >&2
else
  echo "rotated $n leaves. Restart their consumers to pick up the new values, e.g.:" >&2
  echo "  kubectl rollout restart deploy -n <namespace>   # for the affected charts" >&2
fi
