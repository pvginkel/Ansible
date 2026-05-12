FROM ubuntu:questing AS build

ENV POETRY_HOME=/opt/poetry \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml poetry.lock /app/

RUN apt-get update -yqq && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv

RUN pip3 install poetry --break-system-packages && \
    \
    poetry install --no-root --no-interaction --no-ansi

FROM ubuntu:questing

COPY --from=build /app /app

ENV PATH="/home/ubuntu/.npm-global/bin:/home/ubuntu/.local/bin:/app/.venv/bin:${PATH}" \
    KUBE_VERSION=v1.32 \
    TF_CLI_CONFIG_FILE=/etc/terraform.rc

RUN apt-get update -yqq && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        jq \
        openssh-client \
        python3 \
        tini \
        unzip \
        yq \
    && \
    \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN set -o pipefail && \
    \
    curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-4 | bash && \
    \
    curl -fsSL "https://pkgs.k8s.io/core:/stable:/$KUBE_VERSION/deb/Release.key" | gpg --dearmor -o /etc/apt/keyrings/kubernetes.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/kubernetes.gpg] https://pkgs.k8s.io/core:/stable:/$KUBE_VERSION/deb/ /" > /etc/apt/sources.list.d/kubernetes.list && \
    \
    curl -fsSL https://apt.releases.hashicorp.com/gpg | gpg --dearmor -o /etc/apt/keyrings/hashicorp.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/hashicorp.gpg] https://apt.releases.hashicorp.com $(grep -oP '(?<=UBUNTU_CODENAME=).*' /etc/os-release || lsb_release -cs) main" > /etc/apt/sources.list.d/hashicorp.list && \
    \
    apt-get update -yqq && \
    apt-get install -y --no-install-recommends \
        kubectl \
        terraform \
    && \
    \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir /work && \
    chown ubuntu:ubuntu /work

COPY artifacts/terraform-provider-homelab artifacts/terraform-provider-homelab-metadata.json /tmp/
COPY terraform.rc /etc/terraform.rc

RUN VERSION=$(jq -r .version /tmp/terraform-provider-homelab-metadata.json) && \
    INSTALL_DIR=/usr/local/share/terraform/plugins/registry.terraform.io/pvginkel/homelab/$VERSION/linux_amd64 && \
    mkdir -p "$INSTALL_DIR" && \
    mv /tmp/terraform-provider-homelab "$INSTALL_DIR/terraform-provider-homelab_v$VERSION" && \
    chmod +x "$INSTALL_DIR/terraform-provider-homelab_v$VERSION" && \
    rm /tmp/terraform-provider-homelab-metadata.json

ENTRYPOINT ["tini", "--"]
