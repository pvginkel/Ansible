# `iac_agent` role

Configures the `srviac` VM as the homelab's IaC agent — the dedicated host through which all Terraform and Ansible against the cluster flows in production.

Applied to the `iac_agent` group (today: `srviac` only). See [`/work/AnsibleSpecs/phases/iac-agent.md`](../../../AnsibleSpecs/phases/iac-agent.md) for the design.

## What it does

- Installs the Docker engine (`docker.io`) and Compose v2.
- Drops `/etc/docker/daemon.json` declaring `registry:5000` as an insecure registry — the homelab's container registry is HTTP-only.
- Ensures `/etc/iac/` exists and places `secrets.example.yaml` there. **Never overwrites `/etc/iac/secrets.yaml`** — that file is operator-curated, hand-edited on the host. The role fails loudly if `secrets.yaml` is missing so a fresh host surfaces "you need to populate secrets" before anything else runs against bad credentials.
- Syncs the operator's local `IaCAgent` checkout into `/opt/IaCAgent/` (via rsync, `.git` excluded). When the tree changes, runs `install.sh` to materialize `bin/iac`, the systemd unit for the Jenkins inbound agent, the `docker image prune` cron, and friends.

## Depends on

`bootstrap` + `baseline`. `baseline_os_update_class: standalone` is set in `inventories/prd/group_vars/iac_agent.yml` so srviac runs `unattended-upgrades` with auto-reboot in its quiet window.

## Operator inputs

- The `IaCAgent` repo checkout must exist alongside `Ansible` at `{{ iac_agent_local_checkout }}` (default: `/work/IaCAgent` when the controller's playbook dir is `/work/Ansible/ansible/playbooks/`).
- `/etc/iac/secrets.yaml` on the target host — populate by hand once per srviac lifetime, copying from the placed `secrets.example.yaml`. See [phase doc, "secrets.yaml lifecycle"](../../../AnsibleSpecs/phases/iac-agent.md#secretsyaml-lifecycle).

## Carve-out

The `iac-on-push` Jenkins job runs `ansible-playbook playbooks/site.yml --limit '!iac_agent'`. The orchestrator must not mutate itself; changes to this role apply only via the operator workstation.
