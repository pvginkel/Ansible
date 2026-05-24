#!/bin/sh
# Source this from anywhere inside the Ansible repo:
#   . scripts/bao-login.sh
# Reads the openbao-admin AppRole creds from the ansible vault, logs in,
# and exports BAO_ADDR + BAO_TOKEN into the calling shell.

_bao_repo=$(git rev-parse --show-toplevel 2>/dev/null)
if [ -z "$_bao_repo" ]; then
    echo "bao-login: must be sourced from inside the Ansible repo" >&2
    unset _bao_repo
    return 1 2>/dev/null || exit 1
fi

echo "bao-login: reading openbao-admin AppRole creds from the vault..." >&2
_bao_creds=$(cd "$_bao_repo/ansible" && poetry run ansible srvvault1 -m debug \
    -a 'msg="role_id={{ openbao_admin_role_id }} secret_id={{ openbao_admin_secret_id }}"' 2>/dev/null)
if [ $? -ne 0 ] || [ -z "$_bao_creds" ]; then
    echo "bao-login: ansible debug call failed" >&2
    unset _bao_repo _bao_creds
    return 1 2>/dev/null || exit 1
fi

_bao_role_id=$(printf '%s' "$_bao_creds" | sed -n 's/.*role_id=\([^ ]*\) secret_id.*/\1/p')
_bao_secret_id=$(printf '%s' "$_bao_creds" | sed -n 's/.*secret_id=\([^"]*\)".*/\1/p')
if [ -z "$_bao_role_id" ] || [ -z "$_bao_secret_id" ]; then
    echo "bao-login: failed to parse role_id/secret_id from ansible output" >&2
    unset _bao_repo _bao_creds _bao_role_id _bao_secret_id
    return 1 2>/dev/null || exit 1
fi

: "${BAO_ADDR:=https://secrets}"
_bao_token=$(BAO_ADDR="$BAO_ADDR" bao write -field=token auth/approle/login \
    role_id="$_bao_role_id" secret_id="$_bao_secret_id")
if [ -z "$_bao_token" ]; then
    echo "bao-login: approle login failed" >&2
    unset _bao_repo _bao_creds _bao_role_id _bao_secret_id _bao_token
    return 1 2>/dev/null || exit 1
fi

export BAO_ADDR
export BAO_TOKEN="$_bao_token"

_bao_ttl=$(bao token lookup -format=json 2>/dev/null | sed -n 's/.*"ttl": *\([0-9]*\).*/\1/p' | head -n1)
if [ -n "$_bao_ttl" ]; then
    echo "bao-login: BAO_ADDR=$BAO_ADDR  ttl=${_bao_ttl}s" >&2
else
    echo "bao-login: BAO_ADDR=$BAO_ADDR" >&2
fi

unset _bao_repo _bao_creds _bao_role_id _bao_secret_id _bao_token _bao_ttl
