#!/usr/bin/env bash
#
# Materialise the KubeCoder catalog's key material into the files that
# ansible.cfg and ssh expect. Driven from `kc project setup`.
#
# The catalog projects secrets as environment variables, but every consumer
# here wants a path: ansible.cfg pins IdentityFile=~/.ssh/id_ed25519_ansible,
# and ANSIBLE_VAULT_PASSWORD_FILE is a filename by definition. So the values
# have to land on disk with tight modes before anything can use them.
#
# WHERE they land matters as much as the modes. `/home/ubuntu` is a single host
# directory mounted into *every* KubeCoder environment — it carries the Claude
# credential, shell history and dotfiles — so a key written directly there is
# readable from every other environment on the deployment. Each destination
# below therefore sits inside a home *overlay* (`.ssh`, `.ansible`), which is
# this environment's own dataset mounted over the shared home. The overlays are
# declared in .kubecoder/config.yaml; regular tool sidecars carry the home
# stack, so the `iac` container sees these files too.
#
# The guard below only catches a destination placed directly in the shared
# home. It cannot confirm that a subdirectory is genuinely a declared overlay —
# an overlay is not distinguishable from an ordinary directory at runtime (it
# shares a device ID with the home it is mounted over). Keep the destinations
# and the `homeOverlays:` list in config.yaml in step by hand.
#
# Every variable is optional. A checkout that does not select these secrets —
# the operator's workstation, or an environment scoped to something else —
# still gets a clean `kc project setup`; the file is simply left alone.
#
# Nothing here echoes a secret value.

set -euo pipefail

umask 077

# write_secret VAR DEST [trailing-newline]
#
# Writes $VAR to DEST at mode 0600, via a temp file in the same directory so a
# reader never observes a half-written key. Rewrites only when the content
# actually differs, which keeps re-running setup quiet.
write_secret() {
    local var=$1 dest=$2 trailing=${3:-none}
    local value=${!var-}
    local dir

    if [ -z "$value" ]; then
        printf 'keys: %s not set — leaving %s alone\n' "$var" "$dest" >&2
        return 0
    fi

    dir=$(dirname -- "$dest")
    if [ "$dir" = "$HOME" ]; then
        printf 'keys: refusing to write %s directly into the shared home (%s).\n' "$dest" "$HOME" >&2
        printf 'keys: put it inside a directory listed under homeOverlays: in config.yaml.\n' >&2
        return 1
    fi
    mkdir -p -- "$dir"
    chmod 700 -- "$dir"

    # OpenSSH rejects a private key whose final line has no newline; the vault
    # password is compared verbatim, so it must not gain one.
    case "$trailing" in
        newline) value=${value%$'\n'}$'\n' ;;
        none)    value=${value%$'\n'} ;;
    esac

    if [ -f "$dest" ] && [ "$(cat -- "$dest")" = "${value%$'\n'}" ]; then
        chmod 600 -- "$dest"
        printf 'keys: %s already current\n' "$dest" >&2
        return 0
    fi

    local tmp
    tmp=$(mktemp "$dir/.keytmp.XXXXXX")
    printf '%s' "$value" >"$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$dest"
    printf 'keys: wrote %s\n' "$dest" >&2
}

write_secret ANSIBLE_VAULT_PASSWORD "${ANSIBLE_VAULT_PASSWORD_FILE:-$HOME/.ansible/vault-pass}"
write_secret SSH_KEY_ANSIBLE "$HOME/.ssh/id_ed25519_ansible" newline
write_secret SSH_KEY_PVE     "$HOME/.ssh/id_ed25519_pve"     newline
