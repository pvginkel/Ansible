#!/usr/bin/env bash
#
# Materialise the KubeCoder catalog's key material into the files that
# ansible.cfg and ssh expect. Driven from `kc project setup`.
#
# The catalog projects secrets as environment variables, but ssh wants a path:
# ansible.cfg pins IdentityFile=~/.ssh/id_ed25519_ansible. So the keys have to
# land on disk with tight modes before anything can use them.
#
# WHERE they land matters as much as the modes. `/home/ubuntu` is a single host
# directory mounted into *every* KubeCoder environment — it carries the Claude
# credential, shell history and dotfiles — so a key written directly there is
# readable from every other environment on the deployment. Each destination
# below therefore sits inside a home *overlay* (`.ssh`), which is this
# environment's own dataset mounted over the shared home. The overlays are
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
# still gets a clean `kc project setup`; the file is simply left alone, and
# ~/.ssh/config gets no block for it.
#
# Nothing here echoes a secret value.

set -euo pipefail

umask 077

# write_secret VAR DEST
#
# Writes $VAR to DEST at mode 0600, via a temp file in the same directory so a
# reader never observes a half-written key. Rewrites only when the content
# actually differs, which keeps re-running setup quiet.
write_secret() {
    local var=$1 dest=$2
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

    # OpenSSH rejects a private key whose final line has no newline.
    value=${value%$'\n'}$'\n'

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

write_secret SSH_KEY_ANSIBLE "$HOME/.ssh/id_ed25519_ansible"
write_secret SSH_KEY_PVE     "$HOME/.ssh/id_ed25519_pve"

# ssh tries only its default identity file names, so without a config entry a
# plain `ssh root@pve1` or `ssh ansible@<host>` never offers the keys above and
# is refused (publickey). ensure_ssh_config keeps a Match block per key whose
# variable is set, or whose file is mounted, between the BEGIN and END markers
# at the top of ~/.ssh/config, and drops the block of a key that is gone. The
# closing `Match all` hands the rest of the file back to the global scope.
# Every line outside the markers is left as it is.
#
# The pve key is root's on the PVE nodes only: the names are the inventory's
# `proxmox` group, the addresses their LAN addresses (docs/homelab-handover.md),
# for SSH by IP with DNS down. A root login to any other host keeps its default
# and agent keys. Keep the list in step with the inventory.
#
# The Home Assistant key is the one key not written here: config.yaml takes it
# under secretFiles:, so KubeCoder mounts it at /run/secrets/ (root-owned,
# 0440 — ssh's permission check only refuses a group-readable key its own user
# owns), and its block names that path. It logs in as root to the Terminal &
# SSH add-on, whose host key no CA signs, so the block accepts the key ssh
# first sees and pins it in the overlay's known_hosts.
PVE_NODES=pve,pve.home,10.1.0.20,pve1,pve1.home,10.1.0.21,pve2,pve2.home,10.1.0.22
HOMEASSISTANT_KEY=/run/secrets/ssh-key-homeassistant
HOMEASSISTANT_HOSTS=srvhomeassistant,srvhomeassistant.home,10.1.0.5
SSH_CONFIG_BEGIN='# BEGIN kubecoder-keys.sh: rewritten on every run, edit the script instead'
SSH_CONFIG_END='# END kubecoder-keys.sh'

ensure_ssh_config() {
    local dest=$HOME/.ssh/config tmp line state=outside
    local -a block=()

    if [ -n "${SSH_KEY_PVE-}" ]; then
        block+=("Match user root host $PVE_NODES"
            '    IdentityFile ~/.ssh/id_ed25519_pve'
            '    IdentitiesOnly yes')
    fi
    if [ -r "$HOMEASSISTANT_KEY" ]; then
        block+=("Match host $HOMEASSISTANT_HOSTS"
            '    User root'
            "    IdentityFile $HOMEASSISTANT_KEY"
            '    IdentitiesOnly yes'
            '    StrictHostKeyChecking accept-new')
    fi
    if [ -n "${SSH_KEY_ANSIBLE-}" ]; then
        block+=('Match user ansible'
            '    IdentityFile ~/.ssh/id_ed25519_ansible'
            '    IdentitiesOnly yes')
    fi

    if [ ${#block[@]} -eq 0 ] && [ ! -e "$dest" ]; then
        return 0
    fi

    # A mounted key alone leaves write_secret nothing to write, so nothing has
    # made the directory yet.
    mkdir -p -- "$HOME/.ssh"
    chmod 700 -- "$HOME/.ssh"
    tmp=$(mktemp "$HOME/.ssh/.config.XXXXXX")
    {
        if [ ${#block[@]} -gt 0 ]; then
            printf '%s\n' "$SSH_CONFIG_BEGIN" "${block[@]}" 'Match all' "$SSH_CONFIG_END"
        fi
        if [ -e "$dest" ]; then
            while IFS= read -r line || [ -n "$line" ]; do
                if [ "$state" = inside ]; then
                    if [ "$line" = "$SSH_CONFIG_END" ]; then state=outside; fi
                    continue
                fi
                case "$line" in
                    '# BEGIN kubecoder-keys.sh'*) state=inside; continue ;;
                esac
                printf '%s\n' "$line"
            done <"$dest"
        fi
    } >"$tmp"

    # Without its END marker the block's extent is unknown, and dropping the
    # rest of the file would take the operator's own lines with it.
    if [ "$state" = inside ]; then
        rm -f -- "$tmp"
        printf 'keys: %s opens a kubecoder-keys.sh block it never closes; fix it by hand\n' "$dest" >&2
        return 1
    fi

    if [ -e "$dest" ] && cmp -s -- "$tmp" "$dest"; then
        rm -f -- "$tmp"
        if [ ${#block[@]} -gt 0 ]; then
            printf 'keys: ssh config block already current in %s\n' "$dest" >&2
        fi
        return 0
    fi
    mv -f -- "$tmp" "$dest"
    if [ ${#block[@]} -gt 0 ]; then
        printf 'keys: wrote the ssh config block in %s\n' "$dest" >&2
    else
        printf 'keys: removed the ssh config block from %s\n' "$dest" >&2
    fi
}

ensure_ssh_config

# The bpg/proxmox provider uploads cloud-init snippets over SSH with Go's
# x/crypto/ssh, which reads ~/.ssh/known_hosts and honours no
# UserKnownHostsFile. The PVE nodes present step-ca host certificates, so
# without the homelab CA line there a snippet upload fails with "ssh: no
# authorities for hostname: pve.home:22", and a snippet replace has by then
# already deleted the old file. The iac image bakes the same line into root's
# known_hosts (support/iac-image/Dockerfile); this is the environment's half.
#
# The repo file's @cert-authority lines are the host CAs to trust. A homelab
# line the repo file no longer carries is a retired CA and is removed, so a
# rotation (docs/runbooks/ssh-host-ca-rotation.md) can take its old line out
# again. A homelab line is an @cert-authority line whose key comment is
# homelab-ssh-host-ca, the comment every homelab host CA key is generated with.
# Every other line is left as it is.
ensure_host_ca() {
    local src dest=$HOME/.ssh/known_hosts tmp line w keep
    local -a want=()
    src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)/ansible/files/known_hosts.d/homelab
    mkdir -p -- "$HOME/.ssh"
    chmod 700 -- "$HOME/.ssh"
    touch -- "$dest"

    while IFS= read -r line; do
        case "$line" in '@cert-authority '*) want+=("$line") ;; esac
    done <"$src"

    tmp=$(mktemp "$HOME/.ssh/.known_hosts.XXXXXX")
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            '@cert-authority '*' homelab-ssh-host-ca')
                # A loop, not printf | grep -q: grep -q can exit before
                # printf's last write, and under pipefail printf's SIGPIPE
                # would count a wanted line as retired.
                keep=false
                for w in "${want[@]}"; do
                    if [ "$w" = "$line" ]; then keep=true; break; fi
                done
                if [ "$keep" = false ]; then
                    printf 'keys: removed a retired homelab host CA from %s\n' "$dest" >&2
                    continue
                fi
                ;;
        esac
        printf '%s\n' "$line"
    done <"$dest" >"$tmp"

    for line in "${want[@]}"; do
        if grep -qxF -- "$line" "$tmp"; then
            printf 'keys: homelab host CA already in %s\n' "$dest" >&2
        else
            printf '%s\n' "$line" >>"$tmp"
            printf 'keys: added the homelab host CA to %s\n' "$dest" >&2
        fi
    done

    if cmp -s -- "$tmp" "$dest"; then
        rm -f -- "$tmp"
    else
        mv -f -- "$tmp" "$dest"
    fi
}

ensure_host_ca
