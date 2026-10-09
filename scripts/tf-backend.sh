#!/usr/bin/env bash
# Start terraform-backend-git locally for break-glass terraform runs on
# the operator workstation. Binds 127.0.0.1:6061 on the host network, so
# host-side `terraform` uses the exact same backend "http" block that
# srviac's iac container does. Idempotent: a no-op if it's already up.
#
# Image: DockerImages' terraform-backend-git, upstream v0.1.11 with the
# estate's state-race patch, at the build support/iac-image/Dockerfile
# copies the binary from. Break-glass must not depend on registry:5000:
# when the pull fails, the copy of that build cached here runs, and when
# there is none, the stock upstream image runs with a warning on stderr.
# The stock image is acceptable as the last resort because break-glass
# runs while CI and the cluster are down, so the concurrent writers that
# trigger both of its bugs are mostly absent.
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
patched=registry:5000/terraform-backend-git:2601
stock=ghcr.io/plumber-cd/terraform-backend-git:v0.1.11
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

if docker pull -q "$patched" >/dev/null; then
  image=$patched
elif docker image inspect "$patched" >/dev/null 2>&1; then
  image=$patched
  echo "$name: cannot pull $patched; starting the copy cached here" >&2
else
  image=$stock
  cat >&2 <<EOF
$name: ======================================================================
$name: WARNING: $patched is neither pullable nor cached here.
$name: Starting the STOCK backend, $stock, which has two state bugs:
$name:  1. A first read while any other state's lock branch is held can
$name:     wedge the daemon: every request then fails "non-fast-forward
$name:     update". A stale lock branch is enough: a hook or IaC job killed
$name:     mid-run leaves its locks/<state> branch in TerraformState. A restart
$name:     ('docker rm -f $name', rerun this script) helps only once that
$name:     branch is deleted or main has moved on; until then every new
$name:     daemon wedges the same way.
$name:  2. A save that loses a push race to another writer fails, and so does
$name:     every request after it: the change never reaches TerraformState
$name:     (Terraform leaves it in errored.tfstate). It needs another writer
$name:     of TerraformState running at the same time: an Argo CD hook, an
$name:     IaC job or a second terraform run.
$name: ======================================================================
EOF
fi

docker run -d --network host --name "$name" \
  -e GIT_USERNAME -e GITHUB_TOKEN \
  -e TF_BACKEND_HTTP_ENCRYPTION_PROVIDER=sops \
  -e TF_BACKEND_HTTP_SOPS_AGE_RECIPIENTS -e SOPS_AGE_KEY \
  "$image" terraform-backend-git --access-logs

echo "$name up on 127.0.0.1:6061 from $image — 'docker rm -f $name' to stop"
