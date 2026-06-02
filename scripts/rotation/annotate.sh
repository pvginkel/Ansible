#!/usr/bin/env bash
#
# annotate.sh (scripts/rotation/) — classify every kv leaf with two
# custom-metadata attributes that drive rotation:
#
#   rotation            unrestricted | coordinated | external
#   rotation_mechanism  the system/handler used to mint the replacement
#                       (random, postgres, rabbitmq, keycloak, mqtt,
#                        elasticsearch, kibana, ceph-rgw, ceph-cephx,
#                        samba, jenkins, home-assistant, proxmox, ssh,
#                        wifi, dnsmasq, backup-server, android-keystore,
#                        mysql, openai, google, github, mouser, twitter,
#                        torguard, third-party-blob)
#
# `rotation` is the trust class; `rotation_mechanism` is what a rotation
# job dispatches on. Existing `notes` / `rotated_at` are preserved.
#
# Dry-run by default (prints the mapping). Pass --apply to write.
# Unrecognised leaves are reported and skipped (never blind-classified).
# Requires a logged-in bao session + BAO_CACERT, jq.
#
set -uo pipefail
MOUNT="${OPENBAO_KV_MOUNT:-kv}"
APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1

# Leaves that are not real secrets — never annotate.
is_stray() { case "$1" in eso/jenkins-approle|test/*) return 0;; *) return 1;; esac; }

# Map a leaf path to its rotation_mechanism. Order matters: specific
# before general (e.g. */db before *keycloak*, kibana before elastic,
# */app before *mqtt* so zigbee2mqtt/app is `random` not `mqtt`).
mechanism() {
  case "$1" in
    */app|*dhcp-app|*drain-auth|*http-signing) echo random ;;
    */db|*/pgpass)                             echo postgres ;;
    */rabbitmq)                                echo rabbitmq ;;
    *openai*)                                  echo openai ;;
    *gmail*|*google-service-account*)          echo google ;;
    *github*)                                  echo github ;;
    *mouser*)                                  echo mouser ;;
    *twitter*)                                 echo twitter ;;
    *gluetun-wg*)                              echo torguard ;;
    *mydownloads-config*)                      echo third-party-blob ;;
    *oidc*|*keycloak*admin*)                   echo keycloak ;;
    *mqtt*)                                    echo mqtt ;;
    *kibana*)                                  echo kibana ;;
    *elastic*)                                 echo elasticsearch ;;
    *ceph-csi*)                                echo ceph-cephx ;;
    *ceph-rgw*|*/s3)                           echo ceph-rgw ;;
    *samba*|*mydownloads-user*)                echo samba ;;
    *android-keystore*)                        echo android-keystore ;;
    *dns-reservation*|*management-api*)        echo dnsmasq ;;
    *backup-server*)                           echo backup-server ;;
    *homeassistant*)                           echo home-assistant ;;
    *proxmox*)                                 echo proxmox ;;
    *ansible-ssh-key*)                         echo ssh ;;
    *wifi*)                                    echo wifi ;;
    *webathome-org-config*)                    echo mysql ;;
    *jenkins/admin-password*|*version-poller*|*jenkins-agent*) echo jenkins ;;
    *)                                         echo UNKNOWN ;;
  esac
}

# Derive the trust class from the mechanism.
trust() {
  case "$1" in
    random)                                              echo unrestricted ;;
    openai|google|github|mouser|twitter|torguard|third-party-blob) echo external ;;
    *)                                                   echo coordinated ;;
  esac
}

walk() {
  local p="$1" keys k
  if [ -z "$p" ]; then keys=$(bao kv list -format=json -mount="$MOUNT" 2>/dev/null | jq -r '.[]?')
  else keys=$(bao kv list -format=json -mount="$MOUNT" "$p" 2>/dev/null | jq -r '.[]?'); fi
  for k in $keys; do case "$k" in */) walk "$p$k";; *) echo "$p$k";; esac; done
}

printf '%-58s %-12s %s\n' "LEAF" "ROTATION" "MECHANISM"
unknown=0 done=0
while IFS= read -r leaf; do
  [ -n "$leaf" ] || continue
  is_stray "$leaf" && { printf '%-58s %s\n' "$leaf" "(stray — skipped)"; continue; }
  mech=$(mechanism "$leaf"); rot=$(trust "$mech")
  if [ "$mech" = UNKNOWN ]; then printf '%-58s !! UNKNOWN — classify manually\n' "$leaf"; unknown=$((unknown+1)); continue; fi
  printf '%-58s %-12s %s\n' "$leaf" "$rot" "$mech"
  [ "$APPLY" -eq 1 ] || continue
  cm=$(bao kv metadata get -format=json -mount="$MOUNT" "$leaf" 2>/dev/null | jq -c '.data.custom_metadata // {}')
  notes=$(printf '%s' "$cm" | jq -r '.notes // empty')
  rotated=$(printf '%s' "$cm" | jq -r '.rotated_at // empty')
  args=(-mount="$MOUNT" -custom-metadata=rotation="$rot" -custom-metadata=rotation_mechanism="$mech")
  [ -n "$notes" ]   && args+=(-custom-metadata=notes="$notes")
  [ -n "$rotated" ] && args+=(-custom-metadata=rotated_at="$rotated")
  bao kv metadata put "${args[@]}" "$leaf" >/dev/null && done=$((done+1))
done < <(walk "" | sort)

if [ "$APPLY" -eq 1 ]; then echo "annotated $done leaves" >&2; fi
[ "$unknown" -eq 0 ] || echo "WARNING: $unknown unrecognised leaf/leaves left unclassified" >&2
