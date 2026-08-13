#!/usr/bin/env bash
# Idempotent installer for the IaC agent host. Run as root (or via
# sudo); the Ansible iac_agent role calls this script directly.
#
# Layout this script materializes on the target host:
#   /usr/local/bin/iac
#   /usr/local/bin/iac-impl
#   /usr/local/bin/send_message.py
#   /usr/local/bin/jenkins-agent-launch.sh
#   /usr/local/bin/check-protected-vms.sh
#   /usr/local/bin/check-ansible-drift.sh
#   /etc/docker/daemon.json
#   /etc/cron.d/iac-prune
#   /etc/systemd/system/jenkins-agent.service
# /etc/iac/secrets.{yaml,example.yaml} are placed by the Ansible role,
# not by this script.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi

REPO_DIR=$(cd "$(dirname "$0")" && pwd)

changed=0

install_file() {
    # install_file <mode> <src> <dest>
    local mode=$1 src=$2 dest=$3
    if [[ ! -f "$dest" ]] || ! cmp -s "$src" "$dest"; then
        install -D -m "$mode" "$src" "$dest"
        echo "installed $dest"
        changed=1
    fi
}

# yq is needed by jenkins-agent-launch.sh to extract the agent secret
# from /etc/iac/secrets.yaml at start time. apt's `yq` is the Go variant
# whose syntax matches the script.
if ! command -v yq >/dev/null 2>&1; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq yq
    changed=1
fi

install_file 0755 "$REPO_DIR/bin/iac"                          /usr/local/bin/iac
install_file 0755 "$REPO_DIR/bin/iac-impl"                     /usr/local/bin/iac-impl
install_file 0755 "$REPO_DIR/bin/send_message.py"              /usr/local/bin/send_message.py
install_file 0755 "$REPO_DIR/bin/jenkins-agent-launch.sh"      /usr/local/bin/jenkins-agent-launch.sh
install_file 0755 "$REPO_DIR/bin/check-protected-vms.sh"       /usr/local/bin/check-protected-vms.sh
install_file 0755 "$REPO_DIR/bin/check-ansible-drift.sh"       /usr/local/bin/check-ansible-drift.sh
install_file 0644 "$REPO_DIR/etc/docker/daemon.json"           /etc/docker/daemon.json
install_file 0644 "$REPO_DIR/etc/cron.d/iac-prune"             /etc/cron.d/iac-prune

# Track whether the systemd unit changed so we know if a reload is
# warranted.
unit_changed=0
if [[ ! -f /etc/systemd/system/jenkins-agent.service ]] || \
   ! cmp -s "$REPO_DIR/systemd/jenkins-agent.service" /etc/systemd/system/jenkins-agent.service; then
    install -D -m 0644 "$REPO_DIR/systemd/jenkins-agent.service" /etc/systemd/system/jenkins-agent.service
    echo "installed /etc/systemd/system/jenkins-agent.service"
    unit_changed=1
    changed=1
fi

if (( unit_changed )); then
    systemctl daemon-reload
fi

systemctl enable jenkins-agent.service >/dev/null

# Only start when the secrets file is populated. A fresh srviac runs
# this script before the operator hand-edits /etc/iac/secrets.yaml; we
# don't want a thrashing restart loop on missing creds.
should_start=0
if [[ -r /etc/iac/secrets.yaml ]]; then
    jenkins_secret=$(yq -r '.env[] | select(.name == "JENKINS_AGENT_SECRET") | .value' /etc/iac/secrets.yaml 2>/dev/null || true)
    if [[ -n "$jenkins_secret" && "$jenkins_secret" != "null" && "$jenkins_secret" != "REPLACE_ME" ]]; then
        should_start=1
    fi
fi

if (( should_start )); then
    if (( unit_changed )) || ! systemctl is-active --quiet jenkins-agent.service; then
        systemctl restart jenkins-agent.service
    fi
else
    echo "jenkins-agent: JENKINS_AGENT_SECRET missing or still placeholder; not starting"
fi

if (( changed == 0 )); then
    echo "install.sh: nothing to do."
fi
