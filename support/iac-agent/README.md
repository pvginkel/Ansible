# IaCAgent

Host glue for `srviac`, the homelab's IaC orchestrator VM. See [`AnsibleSpecs/phases/iac-agent.md`](https://github.com/pvginkel/AnsibleSpecs/blob/main/phases/iac-agent.md) for the design.

## What's here

| Path | What it is |
|---|---|
| `bin/iac` | The host shim. Acquires `/var/lock/iac.lock` (`flock -w 60`) and runs `iac-impl` inside the `modern-app-dev` container; bind-mounts `iac-impl` and `/etc/iac/secrets.yaml` in. |
| `bin/iac-impl` | The in-container entrypoint. Parses secrets, clones the Ansible + TerraformState repos, syncs state in, runs `poetry install`, executes the caller's command, then syncs state out and pushes any changes back to TerraformState. Bind-mounted in from `/usr/local/bin/iac-impl` on the host (so changes don't require a `modern-app-dev` rebuild). |
| `bin/jenkins-agent-launch.sh` | Wrapper invoked by the systemd unit; extracts `JENKINS_AGENT_SECRET` from `/etc/iac/secrets.yaml` and launches the Jenkins inbound-agent container. |
| `bin/send_message.py` | Push-notification helper (Home Assistant companion app). Adapted from `DesignAssistant/scripts/send_message.py` — rewritten to stdlib `urllib` so it runs inside `modern-app-dev` without the `requests` package. |
| `bin/check-protected-vms.sh` | Used by the on-push and drift Jenkins jobs. Fails when a `terraform plan` proposes destroy/replace on any of the named VMs. |
| `bin/check-ansible-drift.sh` | Used by the drift job. Wraps `ansible-playbook --check --diff` and fails when the recap reports any pending changes. |
| `etc/iac/secrets.example.yaml` | Placeholder for `/etc/iac/secrets.yaml`. The Ansible role places this on a fresh srviac and fails loudly until the operator copies it to `secrets.yaml` and fills in real values. |
| `etc/docker/daemon.json` | Declares `registry:5000` as an insecure registry. |
| `etc/cron.d/iac-prune` | Daily `docker image prune -f` (dangling-only). |
| `systemd/jenkins-agent.service` | Long-running container for the Jenkins inbound agent. |
| `install.sh` | Idempotent installer. Run as root; the Ansible `iac_agent` role calls it via a handler. |

The Jenkins pipelines that drive `srviac` now live in the **Ansible** repo
as `Jenkinsfile.*` (they were moved out of this repo); the controller jobs
check them out from there and run on the `iac-controller`-labelled agent,
holding the IaC mutex via `iac -c`. They lean on this repo's helpers —
`check-protected-vms.sh`, `check-ansible-drift.sh`, `send_message.py` —
which `iac` bind-mounts into the container. Current jobs:

- **`Jenkinsfile.iac-on-push`** — push to `main` on `pvginkel/Ansible`: terraform plan/destroy-check + apply, then Ansible convergence across the `site*.yml` playbooks.
- **`Jenkinsfile.iac-scheduled-update`** — weekly cron: OS-update / patch posture (drain → upgrade → reboot).
- **`Jenkinsfile.iac-scheduled-drift`** — daily cron: terraform + Ansible `--check` drift across the same playbooks, plus the homelab CA root.
- **`Jenkinsfile.iac-dqlite-watchdog`**, **`Jenkinsfile.iac-image`**, **`Jenkinsfile.architecture`** — the k8s-dqlite watch-cache watchdog, the iac container-image build, and the architecture-model job.

The Ansible repo holds the authoritative per-stage breakdown for each.

## Usage on srviac

```sh
iac                              # interactive bash inside the container
iac -c '<shell script>'          # run the script inside the container
iac -v -c '<shell script>'       # same, with iac-impl's setup-progress prints
```

Both hold `/var/lock/iac.lock` via `flock -w 60`. One call = one lock — compose multi-step work into a single `iac -c '…'` rather than chaining calls.

Inside the container `ansible-playbook` and friends are on `$PATH` directly — `iac-impl` runs `poetry install` and resolves the venv via `poetry env info --path`, so callers don't need `poetry run`.

Inside the container, `iac-impl` (bind-mounted in from this repo's `bin/iac-impl`):

1. Parses `/etc/iac/secrets.yaml` — exports `env:` entries; writes `files:` entries at their declared mode.
2. Clones `pvginkel/Ansible` and `pvginkel/TerraformState` into `/work/`.
3. Symlinks `/work/Ansible/terraform/{prd,scratch}/terraform.tfstate` → `/work/TerraformState/{prd,scratch}/terraform.tfstate`.
4. Runs `poetry install --no-root` in `/work/Ansible/ansible/` so `ansible-playbook` is on `$PATH`.
5. Exec's `bash` (interactive) or `sh -c "$SCRIPT"` (the `-c` form).
6. On exit, commits and pushes any state changes in `/work/TerraformState`.

## Operator workstation parity

The same `install.sh` runs on `wrkdev` if you want `iac` parity there — useful for SSH-less local testing. The workstation isn't required to be a parity host; routine usage runs on `srviac` and break-glass uses `terraform` / `ansible-playbook` directly on `wrkdev`.
