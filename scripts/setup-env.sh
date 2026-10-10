# Source me — I export the homelab Terraform provider's credentials into your shell:
#
#   . scripts/bao-login.sh && . scripts/setup-env.sh prd
#
# For planning a deploy repo's Terraform by hand (docs/live-infra-access.md). Argo CD's
# PreSync hook gets the same values from ESO through argocd-hook-credentials; the
# non-secret per-cluster config it runs with (HOMELAB_CEPH_MON_HOST, TF_VAR_zfs_pools, ...)
# is the hook environment in ArgoCDDeploy's config/prd/values.yaml, which a hand plan
# exports itself.
#
# Assumes you are already logged into bao (scripts/bao-login.sh); each value is one
# `bao kv get` against the kv mount. It covers the provider backend credentials (Ceph
# cephx, RGW S3 admin, iac-provisioner token) plus two optional ones. A missing bao leaf
# is a warning, not a hard failure: the shell is left with whatever resolved.
#
# It also pins KUBE_CONFIG_PATH to the cluster's kubeconfig
# (~/.kube/config-<cluster>*, else ~/.kube/config) after verifying that
# kubeconfig really points at the requested cluster — see the kube-target
# block below. A plan also needs the Terraform HTTP state backend up —
# start it first with scripts/tf-backend.sh (it serves 127.0.0.1:6061).

# --- must be sourced, or the exports vanish with the subshell ----------
__se_sourced=0
if [ -n "${BASH_VERSION:-}" ]; then
  (return 0 2>/dev/null) && __se_sourced=1
elif [ -n "${ZSH_VERSION:-}" ]; then
  case "${ZSH_EVAL_CONTEXT:-}" in *:file:*) __se_sourced=1 ;; esac
else
  case "$0" in */setup-env.sh|setup-env.sh) __se_sourced=0 ;; *) __se_sourced=1 ;; esac
fi
if [ "$__se_sourced" -ne 1 ]; then
  echo "setup-env.sh must be sourced: . scripts/setup-env.sh <cluster>" >&2
  exit 1
fi
unset __se_sourced

# --- args --------------------------------------------------------------
__se_cluster="$1"
case "$__se_cluster" in
  dev|prd) ;;
  *)
    echo "usage: . scripts/setup-env.sh <dev|prd>" >&2
    unset __se_cluster
    return 1
    ;;
esac

# On KubeCoder the toolchain lives in the iac sidecar; elsewhere it is local.
__se_iac() {
  if [ -n "${KUBECODER_ENVIRONMENT_ID:-}" ]; then
    cexec iac "$@"
  else
    "$@"
  fi
}

# --- credential map: ENV_VAR  kv-path  property ------------------------
# One combined cephx user per cluster, the RGW admin, and the iac-provisioner
# agent token.
__se_map="
HOMELAB_CEPH_USER             shared/${__se_cluster}/ceph-csi            user_id
HOMELAB_CEPH_KEY              shared/${__se_cluster}/ceph-csi            user_key
HOMELAB_S3_ADMIN_ACCESS_KEY   shared/${__se_cluster}/ceph-rgw/s3        access_key_id
HOMELAB_S3_ADMIN_SECRET_KEY   shared/${__se_cluster}/ceph-rgw/s3        secret_access_key
HOMELAB_IAC_PROVISIONER_TOKEN eso/${__se_cluster}/iac-provisioner/api/token token
"

__se_missing=0
__se_set=0
while read -r __se_var __se_path __se_prop; do
  [ -z "$__se_var" ] && continue
  if __se_val=$(__se_iac bao kv get -mount=kv -field="$__se_prop" "$__se_path" 2>/dev/null) && [ -n "$__se_val" ]; then
    export "$__se_var=$__se_val"
    __se_set=$((__se_set + 1))
  else
    echo "  missing: $__se_var  <-  kv/$__se_path#$__se_prop" >&2
    __se_missing=$((__se_missing + 1))
  fi
done <<EOF
$__se_map
EOF

echo "setup-env: $__se_cluster cluster — exported $__se_set credential(s), $__se_missing missing." >&2

# --- optional: Postgres substrate terraform_admin password ------------
# Only where the substrate exists (dev today). Absence is not an error: a
# cluster without a Postgres substrate just leaves the var unset, and the
# postgresql provider stays unused on releases that don't provision DBs.
__se_pgpw=$(__se_iac bao kv get -mount=kv -field=password "eso/${__se_cluster}/postgres-pas/terraform-admin" 2>/dev/null) || true
if [ -n "${__se_pgpw:-}" ]; then
  export TF_VAR_postgres_admin_password="$__se_pgpw"
  echo "setup-env: exported TF_VAR_postgres_admin_password (Postgres substrate)." >&2
fi
unset __se_pgpw

# --- optional: backup-server management token -------------------------
# Only where a backup-server exists (prd). The homelab provider mints per-scope
# upload credentials (homelab_backup_credential) with it; the matching URL is
# the non-secret HOMELAB_BACKUP_SERVER_URL from the hook environment. Absence is not an
# error: a cluster without a backup-server leaves the var unset and the
# credential resource stays unused. The leaf is the storage release's own
# backup-server management token.
__se_baktok=$(__se_iac bao kv get -mount=kv -field=management_token "eso/${__se_cluster}/storage/prd/backup-server" 2>/dev/null) || true
if [ -n "${__se_baktok:-}" ]; then
  export HOMELAB_BACKUP_SERVER_TOKEN="$__se_baktok"
  echo "setup-env: exported HOMELAB_BACKUP_SERVER_TOKEN (backup-server)." >&2
fi
unset __se_baktok

# --- kube target ------------------------------------------------------
# The homelab provider is now pointed at $__se_cluster, but the kubernetes
# provider follows KUBE_CONFIG_PATH and the CLI falls back to
# ~/.kube/config — so without this a prd deploy silently hits whatever
# that points at (typically dev). Pick ~/.kube/config-<cluster> (else
# ~/.kube/config) and *prove* it is the intended cluster: the kubeconfig
# addresses the apiserver by the node's IP, so confirm that IP is what
# srvk8s1 (prd) / srvk8sdev (dev) resolves to. A mismatch fails the source
# rather than letting a mislabeled kubeconfig through.
case "$__se_cluster" in
  prd) __se_host=srvk8s1 ;;     # 3-node prd; dev is single-node srvk8sdev
  dev) __se_host=srvk8sdev ;;
esac
# Any single ~/.kube/config-<cluster>* serves — the suffix carries the
# access level the host happens to hold (config-prd-write on a KubeCoder
# environment, plain config-prd elsewhere). Two matches is ambiguous, so
# it fails rather than picking one: the wrong guess is a deploy against
# the wrong credentials. `find` and not a glob, because zsh aborts a
# sourced script on an unmatched one.
__se_kube_matches=$(find -L "$HOME/.kube" -maxdepth 1 -type f \
                      -name "config-${__se_cluster}*" 2>/dev/null | sort)
__se_kube=""
__se_kube_n=0
while IFS= read -r __se_cand; do
  [ -z "$__se_cand" ] && continue
  __se_kube="$__se_cand"
  __se_kube_n=$((__se_kube_n + 1))
done <<EOF
$__se_kube_matches
EOF
if [ "$__se_kube_n" -eq 0 ]; then
  __se_kube="$HOME/.kube/config"
fi

__se_kube_ok=0
if [ "$__se_kube_n" -gt 1 ]; then
  echo "setup-env: ERROR — $__se_kube_n kubeconfigs match $HOME/.kube/config-${__se_cluster}*:" >&2
  echo "$__se_kube_matches" | sed 's/^/  /' >&2
  echo "  KUBE_CONFIG_PATH left unset. Leave one, or export KUBE_CONFIG_PATH yourself." >&2
else
  __se_expect_ip=$(getent hosts "$__se_host" | awk 'NR==1{print $1}')
  __se_server=$(__se_iac kubectl --kubeconfig "$__se_kube" config view --minify \
                  -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null)
  __se_server_host=${__se_server#*://}
  __se_server_host=${__se_server_host%%:*}
  __se_server_host=${__se_server_host%%/*}
  case "$__se_server_host" in
    ""|*[!0-9.]*) __se_server_ip=$(getent hosts "$__se_server_host" | awk 'NR==1{print $1}') ;;
    *)            __se_server_ip="$__se_server_host" ;;
  esac

  if [ -n "$__se_expect_ip" ] && [ "$__se_server_ip" = "$__se_expect_ip" ]; then
    export KUBE_CONFIG_PATH="$__se_kube"
    echo "setup-env: kube target $__se_kube — verified $__se_cluster ($__se_host = $__se_expect_ip)." >&2
    __se_kube_ok=1
  else
    echo "setup-env: ERROR — $__se_kube apiserver ${__se_server_host:-<none>} (${__se_server_ip:-?}) is not $__se_host (${__se_expect_ip:-unresolved}); KUBE_CONFIG_PATH left unset. Put the $__se_cluster cluster's kubeconfig at $HOME/.kube/config-${__se_cluster}." >&2
  fi
fi

unset -f __se_iac
unset __se_cluster __se_map __se_var __se_path __se_prop __se_val __se_set \
      __se_host __se_kube __se_kube_matches __se_kube_n __se_cand \
      __se_expect_ip __se_server __se_server_host __se_server_ip
if [ "$__se_missing" -ne 0 ] || [ "$__se_kube_ok" -ne 1 ]; then
  unset __se_missing __se_kube_ok
  return 1
fi
unset __se_missing __se_kube_ok
return 0
