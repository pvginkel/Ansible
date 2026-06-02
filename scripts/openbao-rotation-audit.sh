#!/usr/bin/env bash
#
# openbao-rotation-audit.sh — rotation checklist from kv metadata.
#
# Lists every leaf that still needs a rotation, grouped by
# `rotation_mechanism` (the system/handler you rotate it in). A leaf is
# OFF the list when either:
#   - it has a `rotated_at` marker (already rotated), or
#   - `rotation=unrestricted` (handled by openbao-rotate-unrestricted.sh).
#
# As you build a handler for a mechanism (keycloak, postgres, …), point
# its cronjob at `rotation_mechanism=<x>` and those leaves come off this
# list once they carry `rotated_at`.
#
# READ-ONLY (kv list + metadata get) — reads no secret values, depends on
# nothing in tmp/. Requires a logged-in bao session + BAO_CACERT, jq.
#
# Usage:  . scripts/bao-login.sh ; scripts/openbao-rotation-audit.sh [out.md]
#
set -uo pipefail
MOUNT="${OPENBAO_KV_MOUNT:-kv}"
OUT="${1:-/dev/stdout}"
command -v bao >/dev/null || { echo "bao not found" >&2; exit 1; }
command -v jq  >/dev/null || { echo "jq not found"  >&2; exit 1; }

walk() {
  local p="$1" keys k
  if [ -z "$p" ]; then keys=$(bao kv list -format=json -mount="$MOUNT" 2>/dev/null | jq -r '.[]?')
  else keys=$(bao kv list -format=json -mount="$MOUNT" "$p" 2>/dev/null | jq -r '.[]?'); fi
  for k in $keys; do case "$k" in */) walk "$p$k";; *) echo "$p$k";; esac; done
}

# Not real secrets — kept out of the rotation list (see slice §strays).
is_stray() { case "$1" in eso/jenkins-approle|test/*) return 0;; *) return 1;; esac; }

scratch=$(mktemp); trap 'rm -f "$scratch"' EXIT
needs=0 total=0 unann=0
while IFS= read -r leaf; do
  [ -n "$leaf" ] || continue
  is_stray "$leaf" && continue
  total=$((total + 1))
  cm=$(bao kv metadata get -format=json -mount="$MOUNT" "$leaf" 2>/dev/null | jq -c '.data.custom_metadata // {}')
  [ -n "$(printf '%s' "$cm" | jq -r '.rotated_at // empty')" ] && continue
  rot=$(printf '%s' "$cm" | jq -r '.rotation // empty')
  mech=$(printf '%s' "$cm" | jq -r '.rotation_mechanism // empty')
  [ "$rot" = unrestricted ] && continue
  if [ -z "$rot" ]; then rot="UNANNOTATED"; mech="zz-unannotated"; unann=$((unann+1)); fi
  notes=$(printf '%s' "$cm" | jq -r '.notes // ""')
  printf '%s\t%s\t%s\t%s\n' "${mech:-zz-unset}" "$rot" "$leaf" "$notes" >> "$scratch"
  needs=$((needs + 1))
done < <(walk "" | sort)

{
  echo "# OpenBao secret-rotation checklist"
  echo
  echo "_Generated $(date -I) from kv metadata. Excludes \`rotation=unrestricted\`"
  echo "(auto-rotated by openbao-rotate-unrestricted.sh) and any leaf already"
  echo "stamped \`rotated_at\`. Grouped by \`rotation_mechanism\` = the handler /"
  echo "system you rotate it in. Reads no secret values._"
  echo
  echo "**${needs} leaves still need rotation** (of ${total} total)."
  [ "$unann" -eq 0 ] || echo -e "\n> ⚠ ${unann} leaf/leaves have no \`rotation\` attribute — run openbao-annotate-rotation.sh."
  echo
  echo "## ⚠ Distinct-mint requirements (don't reuse one value)"
  echo "- \`kv/jenkins/mydownloads-android-keystore\` vs \`…/scantopdf-android-keystore\`"
  echo "- \`kv/eso/prd/electronics-inventory/prd/db\` = \`…/guacamole/prd/db\` = \`pgadmin/pgpass\` today — split"
  echo "- \`kv/eso/prd/iot/prd/elastic-credentials\` + filebeat — per-consumer ES accounts"
  echo "- \`kv/eso/prd/iot/prd/mqtt\` + \`kv/jenkins/iot-mqtt\` — per-consumer MQTT accounts"
  echo "- \`kv/eso/prd/design-assistant/{prd,uat,tst,dev}/{db,rabbitmq}\` — distinct per stage"
  echo "- same value across leaves (rotate together): dnsmasq/management-api = iac/dns-reservation;"
  echo "  storage/backup-server = iac/backup-server; version-poller/config holds the jenkins admin token"
  echo
  prev=""
  sort -f "$scratch" | while IFS=$'\t' read -r mech rot leaf notes; do
    if [ "$mech" != "$prev" ]; then printf '\n## %s\n\n' "$mech"; prev="$mech"; fi
    printf -- '- [ ] `kv/%s` _(%s)_%s\n' "$leaf" "$rot" "${notes:+ — $notes}"
    printf '      ```\n'
    printf '      printf %%s "$NEW" | bao kv put -mount=%s %s <key>=-\n' "$MOUNT" "$leaf"
    printf "      bao kv metadata put -mount=%s -custom-metadata=rotated_at=\$(date -I) \\\\\n" "$MOUNT"
    printf "        -custom-metadata=rotation=%s -custom-metadata=rotation_mechanism=%s %s\n" "$rot" "$mech" "$leaf"
    printf '      ```\n'
  done
} > "$OUT"
echo "wrote ${OUT} (${needs}/${total} need rotation)" >&2
