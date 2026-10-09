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

# poetry gets a venv of its own: a system-wide pip install has to
# upgrade Ubuntu's python3-packaging whenever poetry's dependencies
# outgrow it, and pip cannot uninstall an apt-installed package.
RUN python3 -m venv "$POETRY_HOME" && \
    "$POETRY_HOME/bin/pip" install poetry && \
    \
    "$POETRY_HOME/bin/poetry" install --no-root --no-interaction --no-ansi

FROM ubuntu:questing

COPY --from=build /app /app

ENV PATH="/home/ubuntu/.npm-global/bin:/home/ubuntu/.local/bin:/app/.venv/bin:${PATH}" \
    KUBE_VERSION=v1.32 \
    SOPS_VERSION=3.13.3 \
    TERRAFORM_VERSION=1.16.3 \
    TF_CLI_CONFIG_FILE=/etc/terraform.rc

RUN apt-get update -yqq && \
    apt-get install -y --no-install-recommends \
        age \
        ca-certificates \
        curl \
        git \
        gnupg \
        jq \
        librados2 \
        librbd1 \
        openssh-client \
        python3 \
        python3-pip \
        python3-venv \
        tini \
        tzdata \
        unzip \
        yq \
    && \
    \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# poetry on PATH in the final stage, for manual/interactive operator
# use: the build stage's poetry never makes it into the final image.
# Same install as the build stage.
RUN python3 -m venv /opt/poetry && \
    /opt/poetry/bin/pip install poetry && \
    ln -s /opt/poetry/bin/poetry /usr/local/bin/poetry

RUN set -o pipefail && curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh

# SecretRotator: the nightly run of IaC/Scheduled Secret Rotation and the
# operator's `secret-rotator run|plan <path>`. It installs from its prd
# branch, which IaC/SecretRotator resets to the commit it built green
# before it rebuilds this image. kaniko2 builds without a layer cache, so
# every build installs prd's tip. A uv tool has a venv of its own on the
# system Python, which keeps its dependencies out of /app/.venv. The VCS
# install records the commit in direct_url.json (PEP 610), and every run
# logs that commit. The package carries the committed switches.yaml.
RUN UV_TOOL_DIR=/opt/uv/tools UV_TOOL_BIN_DIR=/usr/local/bin \
    uv tool install --no-cache --python /usr/bin/python3 \
        git+https://github.com/pvginkel/SecretRotator@prd

# Trust the homelab step-ca root the same way managed hosts do via the
# baseline role — tools in this image (curl, terraform providers,
# kubectl against internal apiservers) hit step-ca-signed TLS leaves
# and would otherwise fail verification. Same cert artefact as
# roles/baseline/files/homelab-root.crt; -f matches the baseline role's
# forced refresh so later ca-certificates triggers can't drop it.
COPY ansible/roles/baseline/files/homelab-root.crt /usr/local/share/ca-certificates/homelab-root.crt
RUN chmod 644 /usr/local/share/ca-certificates/homelab-root.crt && \
    update-ca-certificates -f

# Python's `requests` / `urllib3` use certifi's bundled trust by
# default, which doesn't carry the homelab root even though
# update-ca-certificates seeded the system trust above. Point them at
# the system bundle so iac-impl's OpenBao client (and any other Python
# TLS code in this image) honours the homelab root. Same trick the
# `hvac` and `requests` docs recommend for private CAs.
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

COPY support/iac-image/smallstep.sources /etc/apt/sources.list.d/smallstep.sources

# The HashiCorp suite below is pinned to `noble` — deliberately NOT this image's own Ubuntu
# codename. HashiCorp only keeps the LTS suites of apt.releases.hashicorp.com populated: on
# 2026-08-04 they deleted `questing` (25.10) outright — an S3 delete marker, hours after a
# build had still installed terraform 1.15.8-1 from it — and `plucky` (25.04) / `oracular`
# (24.10) survive only as empty 20-byte indices. Deriving the codename from /etc/os-release
# therefore breaks this image's build on every interim Ubuntu release, at `apt-get update`
# (exit 100 on the missing Release file), not merely at the terraform install. The debs are
# static Go binaries whose only dependency is `git`, so the noble package IS what the
# questing one was — same 1.15.8-1 — and the pin survives the next base-image bump too.
#
# Terraform is pinned, the same version in every image that installs it: ArgoCDTools'
# argocd-hook, Ansible's support/iac-image, and DockerImages' kube-coder-iac-toolchain. A state is
# stamped with the version that last wrote it, and the Argo CD hook applies states the iac
# toolchain has written, so an unpinned rebuild could leave one side older than the state it
# reads. Bump all three together (AnsibleSpecs decisions.md, "Terraform version").
RUN set -o pipefail && \
    \
    curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-4 | bash && \
    \
    curl -fsSL "https://pkgs.k8s.io/core:/stable:/$KUBE_VERSION/deb/Release.key" | gpg --dearmor -o /etc/apt/keyrings/kubernetes.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/kubernetes.gpg] https://pkgs.k8s.io/core:/stable:/$KUBE_VERSION/deb/ /" > /etc/apt/sources.list.d/kubernetes.list && \
    \
    curl -fsSL https://apt.releases.hashicorp.com/gpg | gpg --dearmor -o /etc/apt/keyrings/hashicorp.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/hashicorp.gpg] https://apt.releases.hashicorp.com noble main" > /etc/apt/sources.list.d/hashicorp.list && \
    \
    curl -fsSL https://packages.smallstep.com/keys/apt/repo-signing-key.gpg -o /etc/apt/keyrings/smallstep.asc && \
    \
    apt-get update -yqq && \
    apt-get install -y --no-install-recommends \
        kubectl \
        step-cli \
        terraform=${TERRAFORM_VERSION}-1 \
    && \
    \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir /work && \
    chown ubuntu:ubuntu /work

COPY support/iac-image/terraform.rc /etc/terraform.rc

# sops — the manual decrypt path for Terraform state, alongside `age`
# (apt, above; it also carries age-keygen). Nothing in a normal run
# touches either: terraform-backend-git below bundles both and
# encrypts/decrypts transparently, which is why the CLIs were absent from
# every host in the estate. What needs them is recovery — TerraformState's
# README documents `sops -d` against a state file under "Decrypting state
# by hand", and that procedure was unrunnable anywhere. age-keygen covers
# the other half: deriving the public recipient from the private key to
# prove the two are one pair, which is D32's invariant.
#
# Not in the Ubuntu archive, so it comes from the GitHub release, and it
# is pinned for the same reason terraform-backend-git is — it is the
# decrypt path for state, and a state-critical component must not move
# underneath an image rebuild. The deb is a static Go binary with no
# dependencies, so dpkg -i needs no apt lists (the block above cleaned
# them).
RUN curl -fsSL -o /tmp/sops.deb \
        "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops_${SOPS_VERSION}_$(dpkg --print-architecture).deb" && \
    dpkg -i /tmp/sops.deb && \
    rm /tmp/sops.deb

# terraform-backend-git — the Git-backed Terraform HTTP state backend.
# iac-impl starts it on 127.0.0.1:6061 and terraform/{prd,scratch} reach
# it via a backend "http" block; the daemon does the pull/push (and
# sops+age encryption) against pvginkel/TerraformState itself. Stock
# v0.1.11 fails writers of that repository that run at once, so this is
# DockerImages' terraform-backend-git image: v0.1.11 with the estate's
# state-race patch. Pinned by that image's build number (not :latest) so
# a state-critical component never moves underneath an image rebuild;
# scripts/tf-backend.sh runs the same build. Only the patched build's
# version carries `+homelab.`.
COPY --from=registry:5000/terraform-backend-git:2601 \
     /usr/bin/terraform-backend-git /usr/bin/terraform-backend-git
RUN terraform-backend-git version | grep -F '+homelab.'

# The bpg/proxmox provider opens an SSH client of its own (snippet
# uploads to /var/lib/vz/snippets/ — the PVE API has no endpoint for
# that), independent of the Ansible ssh_args wired through ansible.cfg.
# Its Go x/crypto/ssh client reads root's ~/.ssh/known_hosts; without
# an @cert-authority line for the homelab SSH host CA the connection
# fails as "ssh: no authorities for hostname" once the PVE node serves
# the step-ca-signed host cert provisioned by the ssh_host_cert role.
# Bake the same committed known_hosts the Ansible runs use, at the
# default path the bpg provider looks for. See AnsibleSpecs slices/
# completed/ssh-host-ca.md.
COPY ansible/files/known_hosts.d/homelab /root/.ssh/known_hosts
RUN chmod 700 /root/.ssh && chmod 644 /root/.ssh/known_hosts

ENTRYPOINT ["tini", "--"]
