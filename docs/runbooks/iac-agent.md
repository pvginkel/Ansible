# IaC agent VM (`srviac`)

The dedicated VM that runs Terraform and Ansible against the homelab in production. After Phase 1 (iac-agent), routine TF + Ansible flows through `srviac`; the operator workstation is reserved for break-glass and for mutating `srviac` itself.

See [`/work/AnsibleSpecs/phases/iac-agent.md`](../../../AnsibleSpecs/phases/iac-agent.md) for the design rationale.

## What lives where

| Where | What |
|---|---|
| `srviac` host | Docker, the `iac` shim, a daily `docker image prune -f` cron, a systemd unit running the Jenkins inbound-agent container, `/etc/iac/secrets.yaml` (operator-curated, `0600`), `/var/lock/iac.lock` (the IaC mutex). |
| `modern-app-dev` image | Terraform, Ansible, kubectl, helm, python, poetry, plus `iac-impl` — the in-container entrypoint that parses `secrets.yaml`, clones the Ansible + TerraformState repos, runs `poetry install`, then exec's whatever you asked for. Built and pushed via the existing DockerImages pipeline. |
| `pvginkel/Ansible` (this repo) | Roles, playbooks, inventory, the Terraform configs (`terraform/{prd,scratch}/`). |
| `pvginkel/TerraformState` | File-based tfstate, cloned + committed per run. Private. Holds the same sensitivity as any secret-bearing repo (VM host private keys, API tokens, proxmox creds). |
| `pvginkel/IaCAgent` | Host glue (`bin/iac`, the systemd unit, `install.sh`, Jenkinsfiles, the `secrets.example.yaml` template). |
| Jenkins controller (`jenkins.webathome.org`) | Three jobs: `iac-on-push`, `iac-scheduled-update`, `iac-scheduled-drift`. All run on the `iac-controller`-labelled agent. |

## Operator workflow

### Routine: push to `main`

A merge to `main` on `pvginkel/Ansible` triggers `iac-on-push`. The job, inside one `iac -c '…'` per stage:

1. Plan-checks `terraform/prd` — fails fast if the plan proposes `replace`/`destroy` on `srviac` (and any other VM names added to `check-protected-vms.sh`'s argument list once Phase 3 picks the OpenBao deployment shape).
2. Applies `terraform/prd`.
3. Runs `site.yml --limit '!iac_agent'`.
4. Runs `update-k8s.yml` (idempotent no-op when no upgrades are pending).

On failure, the post-stage notifies via `send_message.py` with the job name + URL.

### Routine: manual run from `srviac`

SSH in and use `iac`. Two forms, one lock:

```sh
ssh srviac
iac                           # interactive bash inside the container
iac -c 'cd /work/Ansible/ansible && ansible-playbook playbooks/site.yml --check --diff --limit srvxxx'
```

Both acquire `/var/lock/iac.lock` via `flock -w 60`. On contention, the call fails fast (within 60 s) with the holder PID surfaced — there is no waiting; rerun once the holder releases.

Inside the container: `/work/Ansible` and `/work/TerraformState/{prd,scratch}` are fresh clones, `terraform.tfstate` symlinks point into the state repo, every env entry from `secrets.yaml` is exported, and every file entry has been written at its declared mode. **Edits inside an `iac` shell are lost on exit** unless committed and pushed before exiting — same constraint Jenkins jobs run under.

### Break-glass / `srviac` mutation: from `wrkdev`

The orchestrator cannot orchestrate its own replacement. Anything that mutates `srviac` itself runs from `wrkdev`:

- Initial creation: `cd terraform/prd && terraform apply` then `cd ansible && poetry run ansible-playbook playbooks/site.yml --limit srviac`.
- Subsequent agent VM changes (disk resize, role refresh, image bump): same.
- True break-glass (CI down, controller unreachable): `wrkdev` can still run `terraform apply` and `ansible-playbook` directly. **The host-level flock does not see the workstation**, so don't mix routine work between `wrkdev` and `srviac`; that defeats the lock.

## First-time cutover (one-off)

This is the sequence to stand `srviac` up the first time, after all the source code is in place. Each step is run by the operator from `wrkdev` unless stated otherwise.

1. **Create the VM via Terraform.**

   ```sh
   cd terraform/prd && terraform apply
   ```

2. **Apply Ansible to `srviac`** — bootstrap, baseline (including node_exporter + unattended-upgrades), `iac_agent` role.

   ```sh
   cd ansible && poetry run ansible-playbook playbooks/site.yml --diff --limit srviac
   ```

   The role will fail loudly at the secrets step with "you need to populate secrets" — that's expected on a fresh host.

3. **Populate `/etc/iac/secrets.yaml` on `srviac`.**

   ```sh
   ssh srviac
   sudo cp /etc/iac/secrets.example.yaml /etc/iac/secrets.yaml
   sudo chmod 0600 /etc/iac/secrets.yaml
   sudo $EDITOR /etc/iac/secrets.yaml
   ```

   Fill in every `REPLACE_ME` value. The `id_ed25519_ansible` private key body comes from the operator's cloud-synced attachments folder (same identity as `wrkdev` uses today). The `JENKINS_AGENT_SECRET` comes from the controller — register the agent ("IaC Agent", label `iac-controller`, remote root `/work`) on `https://jenkins.webathome.org/` first.

   See [proxmox-credentials.md](proxmox-credentials.md) for the `TF_VAR_proxmox_*` values.

4. **Re-apply the role** to verify it converges cleanly.

   ```sh
   poetry run ansible-playbook playbooks/site.yml --diff --limit srviac
   ```

   The agent container should reach the controller; `systemctl status jenkins-agent` on `srviac` shows it running.

5. **Bootstrap `TerraformState`** from the workstation's current tfstate.

   ```sh
   cp terraform/prd/terraform.tfstate /work/TerraformState/prd/
   cp terraform/scratch/terraform.tfstate /work/TerraformState/scratch/
   cd /work/TerraformState && git add prd scratch && git commit -m 'bootstrap from wrkdev' && git push
   ```

6. **Smoke-test `iac` on `srviac`.**

   ```sh
   ssh srviac
   iac -c 'cd /work/Ansible/terraform/prd && terraform plan'           # should be no-op
   iac -c 'cd /work/Ansible/ansible && ansible-playbook playbooks/site.yml --check --diff --limit "!iac_agent"'
   ```

   Both clean → green light.

7. **Wire the three Jenkins jobs** on the controller (pipeline scripts live in `IaCAgent`). Verify each runs against a no-op change (a comment-only push) before unleashing.

8. **Cutover.** Stop running Terraform and Ansible from `wrkdev` as the routine path. Delete the workstation-local `terraform/{prd,scratch}/terraform.tfstate{,.backup,.<timestamp>.backup}` files — the state repo is authoritative from now on.

## Recovery

### `srviac` is unreachable

- If the host is up but `iac` won't run, check `/var/lock/iac.lock` holder via `fuser -v /var/lock/iac.lock` and `docker ps`.
- If the systemd Jenkins agent is failing, `journalctl -u jenkins-agent -n 100`. Most failures are stale `JENKINS_AGENT_SECRET` (controller regenerated it) or controller unreachable.

### Rebuild `srviac` from scratch

From `wrkdev`:

```sh
cd terraform/prd && terraform apply -replace='module.vm["srviac"]'
cd ../../ansible && poetry run ansible-playbook playbooks/site.yml --diff --limit srviac
# then re-populate /etc/iac/secrets.yaml as in step 3 of cutover
```

Cloud-init re-bakes; the role re-applies; the operator re-populates secrets. The VM's MAC is pinned in Terraform so the dnsmasq reservation keeps the same IP.

### Lost `wrkdev` (extreme case)

Bootstrap any Ubuntu box: install Poetry + the standard SSH keys from the cloud-synced attachments, clone `pvginkel/Ansible`, clone `pvginkel/IaCAgent`, clone `pvginkel/TerraformState`. From there `wrkdev`'s workflows resume. The orchestrator-self-applicable guarantee stops here — there is no zero-touch recovery for the case where both the workstation and `srviac` are lost simultaneously.

## Secret rotation

### `JENKINS_AGENT_SECRET`

Regenerate on the controller, paste into `/etc/iac/secrets.yaml`, `systemctl restart jenkins-agent`.

### `GIT_API_TOKEN` (GitHub PAT for `TerraformState`)

Mint a new PAT (Trello card 20 has the scope), update `/etc/iac/secrets.yaml`. No restart needed; `iac-impl` reads the file at every invocation.

### `TF_VAR_proxmox_password`

Same flow as Phase 0's proxmox-credentials runbook — change on the PVE cluster, update Roboform, then update `/etc/iac/secrets.yaml` on `srviac` and `terraform/prd/terraform.tfvars` on `wrkdev`.

### Ansible SSH key (`id_ed25519_ansible`)

Rotation is in the bootstrap role's "SSH key rotation" section. After rotating, update `secrets.yaml` on `srviac` with the new private key body and `git push` the new public key with the `site.yml` apply.
