#!/usr/bin/env bash
#
# openbao-rotation-audit.sh — generate a secret-rotation checklist from
# OpenBao KV metadata.
#
# Walks every leaf under the kv mount and reports the ones WITHOUT a
# `rotated_at` custom-metadata marker — i.e. values migrated from a
# transcript-exposed source that still need minting fresh. Output is a
# Markdown checklist grouped by the system you rotate in, with each
# leaf's `notes` and a copy-paste rotate + annotate template.
#
# READ-ONLY: uses `bao kv list` + `bao kv metadata get` only — it never
# reads a secret value, so the generated checklist carries no credentials
# and is safe to keep or share. It does NOT depend on anything in tmp/.
#
# Usage:
#   export BAO_CACERT=ansible/roles/baseline/files/homelab-root.crt
#   . scripts/bao-login.sh
#   scripts/openbao-rotation-audit.sh [output.md]      # default: stdout
#
set -uo pipefail
MOUNT="${OPENBAO_KV_MOUNT:-kv}"
OUT="${1:-/dev/stdout}"

command -v bao >/dev/null || { echo "bao not found on PATH" >&2; exit 1; }
command -v jq  >/dev/null || { echo "jq not found on PATH"  >&2; exit 1; }

# Recursively print every leaf path (no trailing slash) under $1.
walk() {
  local prefix="$1" keys k
  if [ -z "$prefix" ]; then
    keys=$(bao kv list -format=json -mount="$MOUNT" 2>/dev/null | jq -r '.[]?') || return 0
  else
    keys=$(bao kv list -format=json -mount="$MOUNT" "$prefix" 2>/dev/null | jq -r '.[]?') || return 0
  fi
  for k in $keys; do
    case "$k" in
      */) walk "${prefix}${k}" ;;
      *)  printf '%s\n' "${prefix}${k}" ;;
    esac
  done
}

# Map a leaf path to the system you mint its replacement in.
classify() {
  case "$1" in
    *oidc*|*keycloak*)           echo "Keycloak" ;;
    *openai*)                    echo "OpenAI" ;;
    *gmail*|*google*|*calendar*) echo "Google" ;;
    *elastic*)                   echo "Elasticsearch" ;;
    *mqtt*)                      echo "MQTT broker" ;;
    *github*)                    echo "GitHub" ;;
    *homeassistant*)             echo "Home Assistant" ;;
    *mouser*)                    echo "Mouser API" ;;
    *twitter*)                   echo "Twitter / X" ;;
    *proxmox*)                   echo "Proxmox" ;;
    *android-keystore*)          echo "Android keystore (keytool)" ;;
    *samba*)                     echo "Samba" ;;
    *wifi*)                      echo "Wi-Fi / router" ;;
    *ceph-rgw*|*/s3)             echo "Ceph RGW" ;;
    *backup-server*)             echo "Backup server" ;;
    *ssh*)                       echo "SSH key" ;;
    */db|*-db|*database*)        echo "Database (Postgres/MySQL)" ;;
    *rabbitmq*|*/app|*secret_key*|*drain-auth*|*http-signing*)
                                 echo "App-internal (generate random)" ;;
    *)                           echo "Review manually" ;;
  esac
}

scratch=$(mktemp)
trap 'rm -f "$scratch"' EXIT
needs=0 total=0
while IFS= read -r leaf; do
  [ -n "$leaf" ] || continue
  total=$((total + 1))
  meta=$(bao kv metadata get -format=json -mount="$MOUNT" "$leaf" 2>/dev/null \
           | jq -c '.data.custom_metadata // {}')
  [ -n "$(printf '%s' "$meta" | jq -r '.rotated_at // empty')" ] && continue
  notes=$(printf '%s' "$meta" | jq -r '.notes // ""')
  printf '%s\t%s\t%s\n' "$(classify "$leaf")" "$leaf" "$notes" >> "$scratch"
  needs=$((needs + 1))
done < <(walk "")

{
  echo "# OpenBao secret-rotation checklist"
  echo
  echo "_Generated $(date -I) from \`kv\` metadata. A leaf with no \`rotated_at\`"
  echo "marker is still transcript-exposed — mint a fresh value, write it, then"
  echo "stamp \`rotated_at\`. Regenerate any time; this reads no secret values._"
  echo
  echo "**${needs} of ${total} leaves need rotation.**"
  echo
  echo "## ⚠ Distinct-mint requirements (don't reuse one value)"
  echo "- \`kv/jenkins/mydownloads-android-keystore\` vs \`…/scantopdf-android-keystore\` — distinct keystores"
  echo "- \`kv/eso/prd/electronics-inventory/prd/db\` vs \`…/guacamole/prd/db\` — distinct (accidentally identical today)"
  echo "- \`kv/eso/prd/iot/prd/elastic-credentials\` + filebeat — per-consumer ES accounts, not the shared \`elastic\` admin"
  echo "- \`kv/eso/prd/iot/prd/mqtt\` + \`kv/jenkins/iot-mqtt\` — per-consumer MQTT accounts"
  echo "- \`kv/eso/prd/design-assistant/{prd,uat,tst,dev}/{db,rabbitmq}\` — distinct per stage"
  echo
  prev=""
  sort -f "$scratch" | while IFS=$'\t' read -r sys leaf notes; do
    if [ "$sys" != "$prev" ]; then printf '\n## %s\n\n' "$sys"; prev="$sys"; fi
    printf -- '- [ ] `kv/%s`%s\n' "$leaf" "${notes:+ — $notes}"
    printf '      ```\n'
    printf '      printf %%s "$NEW" | bao kv put -mount=%s %s <key>=-\n' "$MOUNT" "$leaf"
    printf "      bao kv metadata put -mount=%s -custom-metadata=rotated_at=\$(date -I) %s\n" "$MOUNT" "$leaf"
    printf '      ```\n'
  done
} > "$OUT"

echo "wrote rotation checklist to ${OUT} (${needs}/${total} leaves need rotation)" >&2
