# Sets one public key in an authorized_keys file inside a VM; run as root
# by rotate-ansible-key.yml through `qm guest exec`:
#   sh -c "<this>" sh present|absent apply|check <authorized_keys>
# The key comes on standard input, since sudo logs argv on the PVE node.
# A key is matched by its base64 blob, as ansible.posix.authorized_key
# matches one. Prints "changed" or "ok"; check changes nothing.
set -eu
state=$1 mode=$2 file=$3
read -r type blob comment
[ -f "$file" ] || { echo "$file does not exist" >&2; exit 1; }
if awk -v blob="$blob" '{ for (i = 1; i <= NF; i++) if ($i == blob) found = 1 } END { exit !found }' "$file"; then
  held=present
else
  held=absent
fi
if [ "$held" = "$state" ]; then
  echo ok
  exit 0
fi
if [ "$mode" = apply ]; then
  if [ "$state" = present ]; then
    if [ -n "$(tail -c 1 "$file")" ]; then echo >>"$file"; fi
    printf '%s %s%s\n' "$type" "$blob" "${comment:+ $comment}" >>"$file"
  else
    tmp=$(mktemp "$file.XXXXXX")
    awk -v blob="$blob" '{ for (i = 1; i <= NF; i++) if ($i == blob) next; print }' "$file" >"$tmp"
    chown --reference="$file" "$tmp"
    chmod --reference="$file" "$tmp"
    mv -f "$tmp" "$file"
  fi
fi
echo changed
