# `iac_agent` role

Configures the `srviac` VM as the homelab's IaC agent — the dedicated host through which all Terraform and Ansible against the cluster flows in production.

Applied to the `iac_agent` group (today: `srviac` only). See [`/work/AnsibleSpecs/phases/completed/iac-agent.md`](../../../AnsibleSpecs/phases/completed/iac-agent.md) for the design.

## What it does

- Installs the Docker engine (`docker.io`) and Compose v2.
- Drops `/etc/docker/daemon.json` declaring `registry:5000` as an insecure registry — the homelab's container registry is HTTP-only.
- Ensures `/etc/iac/` exists and places `secrets.example.yaml` there. **Never overwrites `/etc/iac/secrets.yaml`** — that file is operator-curated, hand-edited on the host. The role fails loudly if `secrets.yaml` is missing so a fresh host surfaces "you need to populate secrets" before anything else runs against bad credentials.
- Ensures `/var/lib/iac/ansible-ssh-key/` exists (root, `0700`), the directory of srviac's own copy of the Ansible key. `bin/iac` bind-mounts it writable into every `iac` container and fails while it is missing. The go-live seeds the key and SecretRotator rewrites it; no role or playbook writes it.
- Syncs this repo's `support/iac-agent/` tree into `/opt/IaCAgent/` (via rsync, `.git` excluded) and renders `bin/iac` there from `bin/iac.j2`, which passes `homelab_timezone` into the iac container as `TZ`. When either changes, runs `install.sh` to materialize `bin/iac`, the systemd unit for the Jenkins inbound agent, the `docker image prune` cron, and friends. The tree ships with this repo, so applying the role needs the Ansible checkout and nothing beside it.

## Depends on

`bootstrap` + `baseline`. `baseline_os_update_class: standalone` is set in `inventories/prd/group_vars/iac_agent.yml` so srviac runs `unattended-upgrades` with auto-reboot in its quiet window.

## Operator inputs

- `/etc/iac/secrets.yaml` on the target host — populate by hand once per srviac lifetime, copying from the placed `secrets.example.yaml`. See [the phase doc](../../../AnsibleSpecs/phases/completed/iac-agent.md) for the file's shape and posture.

## Carve-out

The `IaC/Apply` Jenkins job runs `ansible-playbook playbooks/site.yml --limit '!iac_agent'`. The orchestrator must not mutate itself; changes to this role apply only via the operator workstation.
