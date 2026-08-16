#!/usr/bin/env bash
# Launches the Jenkins inbound-agent container. The agent secret lives
# in /etc/iac/secrets.yaml; this script extracts it at start time so
# the systemd unit stays declarative and there's no second secret file.

set -euo pipefail

SECRETS_FILE="/etc/iac/secrets.yaml"
CONTROLLER_URL="https://jenkins.webathome.org/"
AGENT_NAME="IaC Agent"
# Inbound-agent image runs as user `jenkins` (uid 1000) whose $HOME is
# /home/jenkins; that's the only path the agent process can mkdir into.
# The pipelines don't use this workspace — every step shells into the
# iac container, which has its own /work — so anywhere writable works.
AGENT_WORKDIR="/home/jenkins/agent"
AGENT_IMAGE="jenkins/inbound-agent:latest"
CONTAINER_NAME="jenkins-agent"

if [[ ! -r "$SECRETS_FILE" ]]; then
    echo "jenkins-agent-launch: $SECRETS_FILE missing or unreadable" >&2
    exit 1
fi

secret=$(yq -r '.env[] | select(.name == "JENKINS_AGENT_SECRET") | .value' "$SECRETS_FILE")
if [[ -z "$secret" || "$secret" == "null" ]]; then
    echo "jenkins-agent-launch: JENKINS_AGENT_SECRET missing from $SECRETS_FILE" >&2
    exit 1
fi

# This script runs on the host via systemd, outside iac-impl, so it has
# no OpenBao resolver: JENKINS_AGENT_SECRET must be a literal 64-char hex
# secret, never a `!bao …` reference. yq strips the !bao tag and yields
# the bare "mount/path#key" string, which the controller silently rejects
# as an incorrect secret. (A literal is also required so the agent comes
# up at cold boot when OpenBao is down.) Fail loudly instead.
if [[ ! "$secret" =~ ^[0-9a-fA-F]{64}$ ]]; then
    echo "jenkins-agent-launch: JENKINS_AGENT_SECRET must be a literal 64-char hex secret," >&2
    echo "  not '$secret'. Replace any '!bao …' reference with the literal from the" >&2
    echo "  controller's 'IaC Agent' node page, then restart jenkins-agent." >&2
    exit 1
fi

# --group-add grants the container's user access to the host docker
# socket without running the container as root.
docker_gid=$(stat -c %g /var/run/docker.sock)

# The agent container's `sh` steps invoke `iac` directly; mount the
# shim and its sibling helpers in so they're on the agent's PATH.
# /var/lock is mounted so the flock acquired by iac is visible across
# host and agent. The docker socket + binary let iac spawn sibling
# containers as if it were running on the host.
#
# /etc/iac/secrets.yaml is intentionally not mounted into this agent.
# The agent never reads it; iac passes the host path to docker run and
# the daemon (root) mounts it into the iac container directly.
# --init runs tini as PID 1 so orphaned children get reaped. The agent
# JVM would otherwise be PID 1, and a JVM only reaps processes it tracks;
# the `git`/`sh` helpers the durable-task plugin double-forks reparent to
# PID 1 and pile up as zombies (hundreds over the agent's uptime).
exec docker run --rm \
    --name "$CONTAINER_NAME" \
    --init \
    --network host \
    --group-add "$docker_gid" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v /usr/bin/docker:/usr/bin/docker:ro \
    -v /var/lock:/var/lock \
    -v /usr/local/bin/iac:/usr/local/bin/iac:ro \
    -v /usr/local/bin/iac-impl:/usr/local/bin/iac-impl:ro \
    "$AGENT_IMAGE" \
    -url "$CONTROLLER_URL" \
    -name "$AGENT_NAME" \
    -workDir "$AGENT_WORKDIR" \
    -webSocket \
    -secret "$secret"
