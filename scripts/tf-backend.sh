#!/usr/bin/env bash
# Start terraform-backend-git locally for break-glass terraform runs on
# the operator workstation. Binds 127.0.0.1:6061 on the host network, so
# host-side `terraform` uses the exact same backend "http" block that
# srviac's iac container does. Idempotent: a no-op if it's already up.
#
# Credentials: each value is taken from the environment if already set,
# otherwise read from OpenBao (assumes you're logged in already — run
# `. scripts/bao-login.sh` first). The state-backend material lives in
# one leaf, kv/iac/tf-backend, with fields:
#   age_secret_key  — age private key (decrypts state)   -> SOPS_AGE_KEY
#   age_public_key  — age public key   (encrypts state)  -> TF_BACKEND_HTTP_SOPS_AGE_RECIPIENTS
#   github_token    — PAT to push TerraformState         -> GITHUB_TOKEN
# GIT_USERNAME is a constant GitHub accepts alongside a PAT.
#
# Stop it with: docker rm -f tf-backend
set -euo pipefail

name=tf-backend
image=ghcr.io/plumber-cd/terraform-backend-git:v0.1.11
mount=kv
leaf=iac/tf-backend

# need VAR FIELD — echo VAR from the environment, else OpenBao FIELD.
need() {
  local var=$1 field=$2 val
  val=${!var-}
  if [ -z "$val" ]; then
    val=$(bao kv get -mount="$mount" -field="$field" "$leaf") || {
      echo "tf-backend: $var unset and OpenBao $mount/$leaf#$field unreadable" >&2
      echo "            (logged in? run '. scripts/bao-login.sh' first)" >&2
      exit 1
    }
  fi
  printf '%s' "$val"
}

if [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = "true" ]; then
  echo "$name already running on 127.0.0.1:6061"
  exit 0
fi
docker rm -f "$name" >/dev/null 2>&1 || true

export GIT_USERNAME="${GIT_USERNAME:-x-access-token}"
export GITHUB_TOKEN;                        GITHUB_TOKEN=$(need GITHUB_TOKEN github_token)
export TF_BACKEND_HTTP_SOPS_AGE_RECIPIENTS; TF_BACKEND_HTTP_SOPS_AGE_RECIPIENTS=$(need TF_BACKEND_HTTP_SOPS_AGE_RECIPIENTS age_public_key)
export SOPS_AGE_KEY;                        SOPS_AGE_KEY=$(need SOPS_AGE_KEY age_secret_key)

docker run --pull=always --network host --name "$name" \
  -e GIT_USERNAME -e GITHUB_TOKEN \
  -e TF_BACKEND_HTTP_ENCRYPTION_PROVIDER=sops \
  -e TF_BACKEND_HTTP_SOPS_AGE_RECIPIENTS -e SOPS_AGE_KEY \
  "$image" terraform-backend-git --access-logs

echo "$name up on 127.0.0.1:6061 — 'docker rm -f $name' to stop"
