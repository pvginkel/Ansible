#!/usr/bin/env bash
# Put a new login into one remote of storage-prd's rclone.conf (rclone-backup-pvc): for a Google
# Drive remote, the OAuth client's ID and secret and the token `rclone authorize drive` printed; for
# a OneDrive remote, the token `rclone authorize onedrive` printed, alone. It renews an expired or
# revoked login, and moves a Drive remote onto another OAuth client. The procedure, with the login
# that comes first, is docs/runbooks/backup-freshness.md §4.
#
# Run in the Ansible KubeCoder pod, by whoever holds the values:
#
#   scripts/renew-drive-login.sh <remote>
#
# It reads the remote's type from the file first. For a drive section it asks for the client ID,
# and, without echoing, for the client secret (both from Roboform, "Rclone backup Google Drive
# client") and for the token; for a onedrive section, without echoing, for the token alone. The
# token is the one line between "Paste the following into your remote machine --->" and "<---End
# paste". The values travel on stdin into backup-server's container, which has rclone and the claim
# mounted read-write; the secret and the token are never printed, put on a command line or written
# anywhere but the remote's section.
#
# It refuses, writing nothing, unless: the time is 07:00-22:00 UTC at minute :05-:55; no Job in
# storage-prd is active; no rclone process runs in backup-server; the remote is a drive or a
# onedrive section of the file, and a onedrive one names no client of its own (§4 logs it in on
# rclone's built-in client). Then it copies rclone.conf to rclone.conf.pre-<remote>-<UTC stamp> and
# writes in one `rclone config update`: a drive remote's client_id, client_secret and token, leaving
# scope and every other section as they are; a onedrive remote's token, leaving drive_id, drive_type
# and every other section as they are. It prints checks that carry no value.
#
# NS, TARGET and CONF can be overridden only to test against a pod without the claim.
set -euo pipefail

REMOTE=${1:?usage: renew-drive-login.sh <remote>}
NS=${NS:-storage-prd}
TARGET=${TARGET:-deploy/backup-server}
CONF=${CONF:-/data/rclone.conf}
KW="$HOME/.kube/config-prd-write"

case $REMOTE in
  *[!A-Za-z0-9_-]* | -*) echo "REFUSED: '$REMOTE' is not a remote name; nothing written"; exit 1 ;;
esac

TYPE=$(cexec iac kubectl --kubeconfig "$KW" --context prd -n "$NS" exec "$TARGET" -- \
  sed -n "/^\\[$REMOTE\\]\$/,/^\\[/s/^type = //p" "$CONF" </dev/null)
case $TYPE in
  drive | onedrive) ;;
  '') echo "REFUSED: no section [$REMOTE] in $CONF; nothing written"; exit 1 ;;
  *) echo "REFUSED: [$REMOTE] in $CONF is of type $TYPE, not drive or onedrive; nothing written"; exit 1 ;;
esac

echo "remote $REMOTE, type $TYPE, file $CONF in $NS $TARGET"
CLIENT_ID='' CSEC=''
if [ "$TYPE" = drive ]; then
  IFS= read -rp 'Client ID: ' CLIENT_ID
  IFS= read -rsp 'Client secret: ' CSEC; echo >&2
fi
IFS= read -rsp 'Token (one line, starting {"access_token"): ' TOK; echo >&2

if [ "$TYPE" = drive ]; then
  case $CLIENT_ID in
    *[!A-Za-z0-9.-]* | '') echo "REFUSED: the client ID is not one word ending in .apps.googleusercontent.com; nothing written"; exit 1 ;;
    ?*.apps.googleusercontent.com) ;;
    *) echo "REFUSED: the client ID does not end in .apps.googleusercontent.com; nothing written"; exit 1 ;;
  esac
  case $CSEC in
    GOCSPX-?*) ;;
    *) echo "REFUSED: the client secret does not start with GOCSPX-; nothing written"; exit 1 ;;
  esac
fi
case $TOK in
  '{'*'"access_token":"'*'"refresh_token":"'*'}') ;;
  *) echo "REFUSED: the token is not one line of JSON with an access_token and a refresh_token; nothing written"; exit 1 ;;
esac

active=$(cexec iac kubectl --context prd -n "$NS" get jobs \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.active}{"\n"}{end}' </dev/null | awk '$2 > 0 {print $1}')
if [ -n "$active" ]; then
  echo "REFUSED: a Job is running in $NS ($active); nothing written"
  exit 1
fi

INNER=$(cat <<'EOF'
set -eu
if [ "$TYPE" = drive ]; then IFS= read -r CSEC; fi
IFS= read -r TOK
fp() { sed -n 's/.*"refresh_token":"\([^"]*\)".*/\1/p' | sha256sum | cut -c1-12; }
section() { awk -v s="[$REMOTE]" '$0 == s {f = 1; next} /^\[/ {f = 0} f'; }
# The file without the remote's token line: for onedrive, what the write must leave as it was.
rest() { awk -v s="[$REMOTE]" '/^\[/ {f = ($0 == s)} !(f && /^token = /)' "$1" | sha256sum | cut -c1-12; }

h=$(date -u +%H); m=$(date -u +%M); h=${h#0}; m=${m#0}
if [ "$h" -lt 7 ] || [ "$h" -ge 22 ] || [ "$m" -lt 5 ] || [ "$m" -gt 55 ]; then
  echo "REFUSED: $(date -u +%H:%M) UTC is outside 07:00-22:00 at minute :05-:55; nothing written"
  exit 1
fi
for p in /proc/[0-9]*; do
  if [ "$(cat "$p/comm" 2>/dev/null)" = rclone ]; then
    echo "REFUSED: an rclone process (pid ${p#/proc/}) is running; nothing written"
    exit 1
  fi
done
if ! grep -qx "\[$REMOTE\]" "$CONF"; then
  echo "REFUSED: no section [$REMOTE] in $CONF; nothing written"
  exit 1
fi
if [ "$(section < "$CONF" | sed -n 's/^type = //p')" != "$TYPE" ]; then
  echo "REFUSED: [$REMOTE] in $CONF is not of type $TYPE; nothing written"
  exit 1
fi
if [ "$TYPE" = onedrive ] && section < "$CONF" | grep -Eq '^client_(id|secret) = .'; then
  echo "REFUSED: [$REMOTE] names a client of its own, and §4 logs a OneDrive remote in on rclone's built-in client; nothing written"
  exit 1
fi
COPY="$CONF.pre-$REMOTE-$(date -u +%Y%m%dT%H%M%SZ)"
if [ -e "$COPY" ]; then
  echo "REFUSED: $COPY exists already; nothing written"
  exit 1
fi

before=$(grep -c '^\[' "$CONF")
cp -p "$CONF" "$COPY"
if [ "$TYPE" = drive ]; then
  rclone --config "$CONF" config update "$REMOTE" client_id "$CLIENT_ID" client_secret "$CSEC" \
    token "$TOK" config_refresh_token false </dev/null >/dev/null
else
  # --non-interactive: the config questions that follow the token (connection type, drive) are
  # left unasked, so drive_id and drive_type stay as they are.
  rclone --config "$CONF" config update "$REMOTE" token "$TOK" config_refresh_token false \
    --non-interactive </dev/null >/dev/null
fi
at=$(date -u +%FT%TZ)

after=$(grep -c '^\[' "$CONF")
s=$(section < "$CONF")
kv() { printf '%s\n' "$s" | sed -n "s/^$1 = //p"; }
pasted=$(printf '%s\n' "$TOK" | fp)
infile=$(kv token | fp)
[ "$pasted" = "$infile" ] && tokc="matches the paste ($infile)" || tokc="DIFFERS (pasted $pasted, file $infile)"

echo "written at $at"
echo "copy $COPY"
echo "sections: $before before, $after after"
if [ "$TYPE" = drive ]; then
  [ "$(kv client_id)" = "$CLIENT_ID" ] && cid=matches || cid=WRONG
  [ -n "$(kv client_secret)" ] && csec=set || csec=MISSING
  echo "[$REMOTE] type=$(kv type) scope=$(kv scope) client_id=$cid client_secret=$csec"
else
  [ -z "$(kv client_id)" ] && cid=none || cid=SET
  [ "$(rest "$COPY")" = "$(rest "$CONF")" ] && restc=unchanged || restc=CHANGED
  echo "[$REMOTE] type=$(kv type) drive_type=$(kv drive_type) client_id=$cid"
  echo "the file but [$REMOTE]'s token: $restc"
fi
echo "refresh_token fingerprint: $tokc"
ls -l "$CONF" "$COPY" | awk '{print $1, $3, $4, $NF}'
EOF
)

# The client ID is no secret and rides on the command line; the secret and the token go on stdin.
if [ "$TYPE" = drive ]; then
  printf '%s\n%s\n' "$CSEC" "$TOK"
else
  printf '%s\n' "$TOK"
fi | cexec iac kubectl --kubeconfig "$KW" --context prd -n "$NS" \
  exec -i "$TARGET" -- env REMOTE="$REMOTE" TYPE="$TYPE" CLIENT_ID="$CLIENT_ID" CONF="$CONF" \
  sh -c "$INNER"
unset CSEC TOK
