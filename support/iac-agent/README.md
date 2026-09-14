# iac-agent

Host glue for `srviac`, the homelab's IaC orchestrator VM. Part of the Ansible repo, at `support/iac-agent/`: the `iac_agent` role syncs this tree to `/opt/IaCAgent` on srviac and runs its `install.sh`. See [`AnsibleSpecs/phases/completed/iac-agent.md`](https://github.com/pvginkel/AnsibleSpecs/blob/main/phases/completed/iac-agent.md) for the design.

## What's here

| Path | What it is |
|---|---|
| `bin/iac` | The host shim. Runs `iac-impl` inside the `iac` container (`registry:5000/iac:latest`, built from this repo's `support/iac-image/`); bind-mounts four paths in — `iac-impl`, `/etc/iac/secrets.yaml`, `check-protected-vms.sh` and `check-ansible-drift.sh`. |
| `bin/iac-impl` | The in-container entrypoint. Parses secrets, clones the Ansible repo, starts the `terraform-backend-git` daemon on `127.0.0.1:6061` (terraform reaches state through it via each config's `backend.tf` http block), runs `poetry install`, then executes the caller's command. Bind-mounted in from `/usr/local/bin/iac-impl` on the host (so changes don't require an `iac` image rebuild). |
| `bin/jenkins-agent-launch.sh` | Wrapper invoked by the systemd unit; extracts `JENKINS_AGENT_SECRET` from `/etc/iac/secrets.yaml` and launches the Jenkins inbound-agent container. |
| `bin/check-protected-vms.sh` | Used by the on-push, apply and drift Jenkins jobs, against the `terraform/prd` plan JSON. Fails (exit 1) when the plan deletes or replaces any VM; exits 2 on a usage error or an unreadable plan. The second rail: while `managed-vm`'s VM resource carries `prevent_destroy`, `terraform plan` refuses such a plan before the guard runs. |
| `bin/check-ansible-drift.sh` | Used by the drift job. Wraps `ansible-playbook --check --diff` and fails when the recap reports any pending changes. |
| `etc/iac/secrets.example.yaml` | Placeholder for `/etc/iac/secrets.yaml`. The Ansible role places this on a fresh srviac and fails loudly until the operator copies it to `secrets.yaml` and fills in real values. |
| `etc/docker/daemon.json` | Declares `registry:5000` as an insecure registry. |
| `etc/cron.d/iac-prune` | Daily `docker image prune -f` (dangling-only). |
| `systemd/jenkins-agent.service` | Long-running container for the Jenkins inbound agent. |
| `install.sh` | Idempotent installer. Run as root; the Ansible `iac_agent` role calls it via a handler. |

The Jenkins pipelines that drive `srviac` live at the root of this repo as
`Jenkinsfile.*`; the controller jobs check them out from there and run on
the `iac-controller`-labelled agent, doing their work through `iac -c`.
They lean on this tree's helpers — `check-protected-vms.sh` and
`check-ansible-drift.sh` — which `iac` bind-mounts into the container.
Reporting is not one of them: jenkins-telegram-bot watches every build and
reports FAILURE by itself, and where a job needs to say something the build
result does not, it calls JenkinsPipelineUtils' `notify` var. Current jobs:

- **`Jenkinsfile.iac-on-push`** — push to `main` on `pvginkel/Ansible`: read-only validation, `terraform plan` plus the protected-VM destroy check. It converges nothing.
- **`Jenkinsfile.iac-apply`** — the converging half, started by hand: plan, destroy check and apply of that saved plan in one `iac` call, then Ansible convergence across the `site*.yml` playbooks.
- **`Jenkinsfile.iac-scheduled-update`** — weekly cron: OS-update / patch posture for the cluster class (drain → upgrade → reboot).
- **`Jenkinsfile.iac-scheduled-drift`** — daily cron: terraform + Ansible `--check` drift across the same playbooks, plus the homelab CA root.
- **`Jenkinsfile.iac-scheduled-calico`** — weekly cron: rolling restart of the `calico-node` DaemonSet, capping every pod's uptime below the token-refresh stall window.
- **`Jenkinsfile.iac-scheduled-certs`** — weekly cron: certificate renewal against step-ca — the fleet's SSH host certificates and the `internal_tls` X.509 leaves.

**`Jenkinsfile.iac-image`** (the `iac` container-image build) and
**`Jenkinsfile.architecture`** (the architecture-model job) live at the root
too, but run on Kubernetes pod agents rather than on `srviac` and use none
of these helpers.

Each Jenkinsfile's header comment holds the authoritative per-stage
breakdown.

## Usage on srviac

```sh
iac                              # interactive bash inside the container
iac -c '<shell script>'          # run the script inside the container
iac -v -c '<shell script>'       # same, with iac-impl's setup-progress prints
```

Neither form takes a host lock. Terraform state is locked per state by `terraform-backend-git`'s `locks/<state-path>` branches, and the `IaC Agent` node's single executor queues the Jenkins jobs behind one another. The accepted loss: hand-run Ansible on srviac no longer interlocks with a running job (terraform still does, via lock branches). Each call is a fresh container and clone, so compose multi-step work into a single `iac -c '…'` rather than chaining calls.

Inside the container `ansible-playbook` and friends are on `$PATH` directly — `iac-impl` runs `poetry install` and resolves the venv via `poetry env info --path`, so callers don't need `poetry run`.

Inside the container, `iac-impl` (bind-mounted in from this tree's `bin/iac-impl`):

1. Parses `/etc/iac/secrets.yaml` — exports `env:` entries; writes `files:` entries at their declared mode.
2. Clones `pvginkel/Ansible` into `/work/`.
3. Starts the `terraform-backend-git` daemon on `127.0.0.1:6061`; terraform reaches state via the `backend.tf` http block in `terraform/{prd,scratch}/`. The daemon does the git pull/push against `pvginkel/TerraformState` itself and encrypts state at rest with sops + age.
4. Runs `poetry install --no-root` in `/work/Ansible/ansible/` so `ansible-playbook` is on `$PATH`.
5. Exec's `bash` (interactive) or `sh -c "$SCRIPT"` (the `-c` form).

## Operator workstation parity

The same `install.sh` runs on `wrkdev` if you want `iac` parity there — useful for SSH-less local testing. The workstation isn't required to be a parity host; routine usage runs on `srviac` and break-glass uses `terraform` / `ansible-playbook` directly on `wrkdev`.
