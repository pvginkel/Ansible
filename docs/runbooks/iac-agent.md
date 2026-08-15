# IaC agent VM (`srviac`)

The dedicated VM that runs Terraform and Ansible against the homelab in production. After Phase 1 (iac-agent), routine TF + Ansible flows through `srviac`; the operator workstation is reserved for break-glass and for mutating `srviac` itself.

See [`/work/AnsibleSpecs/phases/completed/iac-agent.md`](../../../AnsibleSpecs/phases/completed/iac-agent.md) for the design rationale.

## What lives where

| Where | What |
|---|---|
| `srviac` host | Docker, the `iac` shim, a daily `docker image prune -f` cron, a systemd unit running the Jenkins inbound-agent container, `/etc/iac/secrets.yaml` (operator-curated, `0600`), `/var/lock/iac.lock` (the IaC mutex). |
| `registry:5000/iac` image | Terraform, Ansible, kubectl, helm, python, poetry, `terraform-backend-git`, plus `iac-impl` — the in-container entrypoint that parses `secrets.yaml`, clones the repos `secrets.yaml` names (Ansible alone by default), starts the terraform-backend-git daemon on `127.0.0.1:6061`, then exec's whatever you asked for. The Python venv is baked in at image build from this repo's `pyproject.toml`/`poetry.lock`; `iac-impl` installs nothing at runtime and instead warns when the cloned `poetry.lock` differs from the baked one. Built from `support/iac-image/Dockerfile` by this repo's `iac-image` job. |
| `pvginkel/Ansible` (this repo) | Roles, playbooks, inventory, the Terraform configs (`terraform/{prd,scratch}/`, each with a `backend.tf` http block), the Jenkins pipeline scripts (`Jenkinsfile.*`) every job checks out, the iac image's build context (`support/iac-image/`), and the srviac host glue (`support/iac-agent/` — `bin/iac`, `install.sh`, the systemd unit, the `secrets.example.yaml` template). |
| `pvginkel/TerraformState` | tfstate served through the terraform-backend-git http backend, sops+age-encrypted at rest. Private. Holds the same sensitivity as any secret-bearing repo (VM host private keys, API tokens, proxmox creds). Not srviac's alone: the Argo CD Terraform PreSync hook (`/work/ArgoCDTools`) writes into the same repo under `argocd/<repo>/<stage>/terraform.tfstate`, starting its own terraform-backend-git in the hook pod. Both sides decrypt with the one age keypair at `kv/iac/tf-backend`: the prd `eso` AppRole is granted read on that single leaf rather than a copy being made, so the two cannot drift onto different keys. |
| Jenkins controller (`jenkins.webathome.org`) | Six jobs on the `iac-controller`-labelled agent: `iac-on-push`, `iac-apply`, `iac-scheduled-update`, `iac-scheduled-drift`, `iac-scheduled-calico`, `iac-scheduled-certs`. `iac-image` and `architecture` also build from this repo, but on Kubernetes pod agents — they hold no IaC mutex and use none of the host glue. |

## Operator workflow

### Routine: push to `main`, then apply

**Pushing and applying are two acts.** A merge to `main` on `pvginkel/Ansible` triggers
`iac-on-push`, which is read-only: it plan-checks `terraform/prd` and fails fast if the plan
proposes `replace`/`destroy` on `srviac` (or any other VM name in `check-protected-vms.sh`'s
argument list). Nothing converges. A red build means the commit would not apply cleanly; the estate
is untouched either way.

Convergence is `iac-apply`, started by hand once the validation is green. The job, inside one
`iac -c '…'` per stage:

1. Repeats the plan + destroy check — the guard has to run against the commit being applied, not a
   different build.
2. Applies `terraform/prd`.
3. Runs `site.yml --limit '!iac_agent'`.
4. Runs `site-openbao.yml`.
5. Runs `site-k8s.yml --limit k8s_prd`.
6. Converges `srvk8sdev` last, in a stage that can only ever downgrade the build to UNSTABLE.

All ansible stages pass `--skip-tags os_update`: patch posture belongs to `iac-scheduled-update`.

On failure, the post-stage notifies via `send_message.py` with the job name + URL.

> The split exists so that pushing a commit is not the same act as applying it to production — an
> unattended agent pushing a branch must not be able to roll the prd fleet. The cost is that **prd
> no longer converges on its own after a push**: if you push and do not start `iac-apply`, the
> change sits unapplied until a scheduled job or a later apply picks it up.

### Routine: the `iac` image rebuild

A push still starts `iac-image`, but the job decides for itself whether to build. It rebuilds only
when the push's changeset touched something the image is built from — `support/iac-image/`, the
root `pyproject.toml` or `poetry.lock` whose venv is baked in,
`ansible/roles/baseline/files/homelab-root.crt`, `ansible/files/known_hosts.d/homelab`, or
`Jenkinsfile.iac-image` itself, which carries the Dockerfile path, the context and both tags. Any
other push reports the `Building iac image` stage as *skipped for conditional* and pushes no tag;
`registry:5000/iac:latest` stays where it was.

The gate reads the build's own changeset (`utils.hasChanges`), which has two consequences worth
knowing:

- A build that **fails** on an image-input push is not retried by the next unrelated push — the
  input no longer appears in that build's changeset. Restart the failed build, or push again.
- A build whose changeset is **empty** always builds. That is deliberate: it is how a rebuild
  started with no new commits — by hand, or automatically to refresh the image's floating base
  layers — still gets through.

So: to force a rebuild, push a trivial change under `support/iac-image/`, or start the job by hand
when there are no new commits since its last build. There is no force parameter.

### Routine: manual run from `srviac`

SSH in and use `iac`. Two forms, one lock:

```sh
ssh srviac
iac                           # interactive bash inside the container
iac -c 'cd /work/Ansible/ansible && ansible-playbook playbooks/site.yml --limit srvxxx --check'
```

Both acquire `/var/lock/iac.lock` via `flock -w 60`. On contention, the call fails fast (within 60 s) with the holder PID surfaced — there is no waiting; rerun once the holder releases.

Inside the container: `/work/Ansible` is a fresh clone, terraform state flows through the local terraform-backend-git daemon (the `backend.tf` http block in each config — no symlinks, no `/work/TerraformState` checkout in the container), every env entry from `secrets.yaml` is exported, and every file entry has been written at its declared mode. **Edits inside an `iac` shell are lost on exit** unless committed and pushed before exiting — same constraint Jenkins jobs run under.

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
   cd ansible && poetry run ansible-playbook playbooks/site.yml --limit srviac
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
   poetry run ansible-playbook playbooks/site.yml --limit srviac
   ```

   The agent container should reach the controller; `systemctl status jenkins-agent` on `srviac` shows it running.

5. **Bootstrap `TerraformState`** from the workstation's current tfstate. State now lives behind the terraform-backend-git http backend and is sops+age-encrypted at rest; the first apply through the backend writes it there. The one-time seed from the workstation's plaintext files:

   ```sh
   cp terraform/prd/terraform.tfstate /work/TerraformState/prd/
   cp terraform/scratch/terraform.tfstate /work/TerraformState/scratch/
   cd /work/TerraformState && git add prd scratch && git commit -m 'bootstrap from wrkdev' && git push
   ```

   These seed files are plaintext; the backend re-encrypts each on its next write.

6. **Smoke-test `iac` on `srviac`.**

   ```sh
   ssh srviac
   iac -c 'cd /work/Ansible/terraform/prd && terraform plan'           # should be no-op
   iac -c 'cd /work/Ansible/ansible && ansible-playbook playbooks/site.yml --limit "!iac_agent" --check'
   ```

   Both clean → green light.

7. **Wire the six `iac-controller` jobs** on the controller — each is a pipeline job with SCM `pvginkel/Ansible` and Script Path `Jenkinsfile.iac-<name>` in the repo root. Verify each runs against a no-op change (a comment-only push) before unleashing.

8. **Cutover.** Stop running Terraform and Ansible from `wrkdev` as the routine path. Delete the workstation-local `terraform/{prd,scratch}/terraform.tfstate{,.backup,.<timestamp>.backup}` files — state is reached only through the backend (encrypted in `TerraformState`) from now on.

## Recovery

### `srviac` is unreachable

- If the host is up but `iac` won't run, check `/var/lock/iac.lock` holder via `fuser -v /var/lock/iac.lock` and `docker ps`.
- If the systemd Jenkins agent is failing, `journalctl -u jenkins-agent -n 100`. Most failures are stale `JENKINS_AGENT_SECRET` (controller regenerated it) or controller unreachable.

### Rebuild `srviac` from scratch

From `wrkdev`:

```sh
cd terraform/prd && terraform apply -replace='module.vm["srviac"]'
cd ../../ansible && poetry run ansible-playbook playbooks/site.yml --limit srviac
# then re-populate /etc/iac/secrets.yaml as in step 3 of cutover
```

Cloud-init re-bakes; the role re-applies; the operator re-populates secrets. The VM's MAC is pinned in Terraform so the dnsmasq reservation keeps the same IP.

### Lost `wrkdev` (extreme case)

Bootstrap any Ubuntu box: install Poetry + the standard SSH keys from the cloud-synced attachments, clone `pvginkel/Ansible` — which carries the host glue at `support/iac-agent/`, so that one clone is the whole controller side. For break-glass terraform, run `scripts/tf-backend.sh` (the same backend in a local `docker run --network host`) and have the age private key from OpenBao (`kv/iac/tf-backend#age_secret_key`) so the backend can decrypt state — `wrkdev` doesn't clone `TerraformState` for normal use. From there `wrkdev`'s workflows resume. The orchestrator-self-applicable guarantee stops here — there is no zero-touch recovery for the case where both the workstation and `srviac` are lost simultaneously.

## Secret rotation

### `JENKINS_AGENT_SECRET`

Regenerate on the controller, paste into `/etc/iac/secrets.yaml`, `systemctl restart jenkins-agent`.

### `GIT_API_TOKEN` (GitHub PAT for `TerraformState`)

Mint a new PAT (Trello card 20 has the scope), update `/etc/iac/secrets.yaml`. Now consumed by the terraform-backend-git daemon (as its `GITHUB_TOKEN`) to pull/push `TerraformState`. No restart needed; `iac-impl` reads the file at every invocation and starts the daemon fresh per run.

### `TF_VAR_proxmox_password`

Same flow as Phase 0's proxmox-credentials runbook — change on the PVE cluster, update Roboform, then update `/etc/iac/secrets.yaml` on `srviac` and `terraform/prd/terraform.tfvars` on `wrkdev`.

### State encryption keypair (SOPS/age)

`TF_BACKEND_HTTP_SOPS_AGE_RECIPIENTS` (the public half, a literal in `/etc/iac/secrets.yaml`) and `SOPS_AGE_KEY` (the private half, `!bao kv/iac/tf-backend#age_secret_key`) are one keypair. D32 makes that an estate invariant rather than a detail: `iac` and the Argo CD PreSync hook both write `pvginkel/TerraformState`, so a second keypair would leave state one side cannot decrypt.

**Reading the recipient** is the usual need — every new consumer takes it as a plaintext literal, and it is a public key, not a secret:

```
ssh srviac 'sudo grep -A1 TF_BACKEND_HTTP_SOPS_AGE_RECIPIENTS /etc/iac/secrets.yaml'
```

Deriving it from the private half with `age-keygen -y` is the documented alternative and the worse one: **`age`, `age-keygen` and `sops` are installed on none of `srviac`, the KubeCoder `iac` sidecar, or the dev container** — terraform-backend-git carries them inside its own image — so it means fetching a binary *and* handling the private key to recover a string already sitting in plaintext. Reserve it for proving the two halves match, on a box where `age` exists:

```
bao kv get -field=age_secret_key -mount=kv iac/tf-backend | age-keygen -y
```

Pipe it, never `bao kv get` first and paste — that puts the private key in scrollback and history. Compare the `age1…` it prints against the literal above; a mismatch means state written by one side is undecryptable by the other, which is D32's failure mode.

**Rotation is not a paste.** Every tfstate already in `TerraformState` is encrypted to the current recipient, so swapping both halves at once orphans all existing state. The shape to use instead is a transition: encrypt to old *and* new (the variable is `RECIPIENTS`, plural — sops takes a comma-separated age list, and `SOPS_AGE_KEY` takes multiple identities), let every state get rewritten by a normal apply, then drop the old. Dry-run it against one throwaway state before touching prd — this path has not been exercised.

### Ansible SSH key (`id_ed25519_ansible`)

Rotation is in the bootstrap role's "SSH key rotation" section. After rotating, update `secrets.yaml` on `srviac` with the new private key body and `git push` the new public key with the `site.yml` apply.
