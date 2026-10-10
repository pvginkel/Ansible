#!/bin/sh
# Source this from anywhere inside the Ansible repo:
#   . scripts/bao-login.sh
# Reads the openbao-admin AppRole creds from the ansible vault, logs in,
# and exports BAO_ADDR + BAO_TOKEN into the calling shell.

# Resolve the repo root relative to this script's location. When sourced,
# $0 is the calling shell, so prefer the shell's own source-file variable.
if [ -n "$BASH_SOURCE" ]; then
    _bao_src=$BASH_SOURCE
else
    _bao_src=$0
fi
_bao_repo=$(cd "$(dirname "$_bao_src")/.." && pwd)
if [ -z "$_bao_repo" ] || [ ! -d "$_bao_repo/ansible" ]; then
    echo "bao-login: could not locate the Ansible repo root from $_bao_src" >&2
    unset _bao_repo _bao_src
    return 1 2>/dev/null || exit 1
fi

# On KubeCoder the toolchain lives in the iac sidecar; elsewhere it is local.
if [ -n "$KUBECODER_ENVIRONMENT_ID" ]; then
    _bao_cexec="cexec iac"
else
    _bao_cexec=
fi

echo "bao-login: reading openbao-admin AppRole creds from the vault..." >&2
_bao_creds=$(cd "$_bao_repo/ansible" && $_bao_cexec poetry run ansible srvvault1 -m debug \
    -a 'msg="role_id={{ openbao_admin_role_id }} secret_id={{ openbao_admin_secret_id }}"' 2>/dev/null)
if [ $? -ne 0 ] || [ -z "$_bao_creds" ]; then
    echo "bao-login: ansible debug call failed" >&2
    unset _bao_repo _bao_src _bao_cexec _bao_creds
    return 1 2>/dev/null || exit 1
fi

_bao_role_id=$(printf '%s' "$_bao_creds" | sed -n 's/.*role_id=\([^ ]*\) secret_id.*/\1/p')
_bao_secret_id=$(printf '%s' "$_bao_creds" | sed -n 's/.*secret_id=\([^" ]*\).*/\1/p')
if [ -z "$_bao_role_id" ] || [ -z "$_bao_secret_id" ]; then
    echo "bao-login: failed to parse role_id/secret_id from ansible output" >&2
    unset _bao_repo _bao_src _bao_cexec _bao_creds _bao_role_id _bao_secret_id
    return 1 2>/dev/null || exit 1
fi

: "${BAO_ADDR:=https://secrets}"
# secret_id=- reads the value from stdin, so it is on no argv; printf is a
# builtin.
_bao_token=$(printf '%s' "$_bao_secret_id" | BAO_ADDR="$BAO_ADDR" $_bao_cexec bao write -field=token \
    auth/approle/login role_id="$_bao_role_id" secret_id=-)
if [ -z "$_bao_token" ]; then
    echo "bao-login: approle login failed" >&2
    unset _bao_repo _bao_src _bao_cexec _bao_creds _bao_role_id _bao_secret_id _bao_token
    return 1 2>/dev/null || exit 1
fi

export BAO_ADDR
export BAO_TOKEN="$_bao_token"

_bao_ttl=$($_bao_cexec bao token lookup -format=json 2>/dev/null | sed -n 's/.*"ttl": *\([0-9]*\).*/\1/p' | head -n1)
if [ -n "$_bao_ttl" ]; then
    echo "bao-login: BAO_ADDR=$BAO_ADDR  ttl=${_bao_ttl}s" >&2
else
    echo "bao-login: BAO_ADDR=$BAO_ADDR" >&2
fi

unset _bao_repo _bao_src _bao_cexec _bao_creds _bao_role_id _bao_secret_id _bao_token _bao_ttl
