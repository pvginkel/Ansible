#!/usr/bin/env bash
# Put a new Google Drive login into one remote of storage-prd's rclone.conf (rclone-backup-pvc):
# the OAuth client's ID and secret and the token `rclone authorize drive` printed. It renews an
# expired or revoked login, and moves a remote onto another OAuth client. The procedure, with the
# login that comes first, is docs/runbooks/backup-freshness.md §4.
#
# Run in the Ansible KubeCoder pod, by whoever holds the values:
#
#   scripts/renew-drive-login.sh <remote>
#
# It asks for the client ID, and, without echoing, for the client secret (both from Roboform,
# "Rclone backup Google Drive client") and for the one line between "Paste the following into your
# remote machine --->" and "<---End paste". The values travel on stdin into backup-server's
# container, which has rclone and the claim mounted read-write; the secret and the token are never
# printed, put on a command line or written anywhere but the remote's section.
#
# It refuses, writing nothing, unless: the time is 07:00-22:00 UTC at minute :05-:55; no Job in
# storage-prd is active; no rclone process runs in backup-server; the remote is a drive section of
# the file. Then it copies rclone.conf to rclone.conf.pre-<remote>-<UTC stamp>, sets the remote's
# client_id, client_secret and token in one `rclone config update`, leaving scope and every other
# section as they are, and prints checks that carry no value.
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

echo "remote $REMOTE, file $CONF in $NS $TARGET"
IFS= read -rp 'Client ID: ' CLIENT_ID
IFS= read -rsp 'Client secret: ' CSEC; echo >&2
IFS= read -rsp 'Token (one line, starting {"access_token"): ' TOK; echo >&2

case $CLIENT_ID in
  *[!A-Za-z0-9.-]* | '') echo "REFUSED: the client ID is not one word ending in .apps.googleusercontent.com; nothing written"; exit 1 ;;
  ?*.apps.googleusercontent.com) ;;
  *) echo "REFUSED: the client ID does not end in .apps.googleusercontent.com; nothing written"; exit 1 ;;
esac
case $CSEC in
  GOCSPX-?*) ;;
  *) echo "REFUSED: the client secret does not start with GOCSPX-; nothing written"; exit 1 ;;
esac
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
IFS= read -r CSEC
IFS= read -r TOK
fp() { sed -n 's/.*"refresh_token":"\([^"]*\)".*/\1/p' | sha256sum | cut -c1-12; }
section() { awk -v s="[$REMOTE]" '$0 == s {f = 1; next} /^\[/ {f = 0} f'; }

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
if [ "$(section < "$CONF" | sed -n 's/^type = //p')" != drive ]; then
  echo "REFUSED: [$REMOTE] in $CONF is not of type drive; nothing written"
  exit 1
fi
COPY="$CONF.pre-$REMOTE-$(date -u +%Y%m%dT%H%M%SZ)"
if [ -e "$COPY" ]; then
  echo "REFUSED: $COPY exists already; nothing written"
  exit 1
fi

before=$(grep -c '^\[' "$CONF")
cp -p "$CONF" "$COPY"
rclone --config "$CONF" config update "$REMOTE" client_id "$CLIENT_ID" client_secret "$CSEC" \
  token "$TOK" config_refresh_token false </dev/null >/dev/null
at=$(date -u +%FT%TZ)

after=$(grep -c '^\[' "$CONF")
s=$(section < "$CONF")
kv() { printf '%s\n' "$s" | sed -n "s/^$1 = //p"; }
[ "$(kv client_id)" = "$CLIENT_ID" ] && cid=matches || cid=WRONG
[ -n "$(kv client_secret)" ] && csec=set || csec=MISSING
pasted=$(printf '%s\n' "$TOK" | fp)
infile=$(kv token | fp)
[ "$pasted" = "$infile" ] && tokc="matches the paste ($infile)" || tokc="DIFFERS (pasted $pasted, file $infile)"

echo "written at $at"
echo "copy $COPY"
echo "sections: $before before, $after after"
echo "[$REMOTE] type=$(kv type) scope=$(kv scope) client_id=$cid client_secret=$csec"
echo "refresh_token fingerprint: $tokc"
ls -l "$CONF" "$COPY" | awk '{print $1, $3, $4, $NF}'
EOF
)

# The client ID is no secret and rides on the command line; the secret and the token go on stdin.
printf '%s\n%s\n' "$CSEC" "$TOK" | cexec iac kubectl --kubeconfig "$KW" --context prd -n "$NS" \
  exec -i "$TARGET" -- env REMOTE="$REMOTE" CLIENT_ID="$CLIENT_ID" CONF="$CONF" \
  sh -c "$INNER"
unset CSEC TOK
