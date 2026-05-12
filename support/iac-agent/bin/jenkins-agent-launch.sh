#!/usr/bin/env bash
# Launches the Jenkins inbound-agent container. The agent secret lives
# in /etc/iac/secrets.yaml; this script extracts it at start time so
# the systemd unit stays declarative and there's no second secret file.

set -euo pipefail

SECRETS_FILE="/etc/iac/secrets.yaml"
CONTROLLER_URL="https://jenkins.webathome.org/"
AGENT_NAME="IaC Agent"
AGENT_WORKDIR="/work"
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

# --group-add grants the container's user access to the host docker
# socket without running the container as root.
docker_gid=$(stat -c %g /var/run/docker.sock)

exec docker run --rm \
    --name "$CONTAINER_NAME" \
    --network host \
    --group-add "$docker_gid" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v /usr/bin/docker:/usr/bin/docker:ro \
    -v /var/lock:/var/lock \
    -v "$SECRETS_FILE:$SECRETS_FILE:ro" \
    "$AGENT_IMAGE" \
    -url "$CONTROLLER_URL" \
    -name "$AGENT_NAME" \
    -workDir "$AGENT_WORKDIR" \
    -webSocket \
    "$secret"
