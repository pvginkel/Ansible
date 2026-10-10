# Slice testing strategy

How a slice is proven once its phases are merged. The run loop's test phase is "read this doc and
execute it".

**The Ansible roles and the Terraform have no runnable test suite, and that is a decision, not a
gap.** What Ansible and Terraform do is converge real machines; the only honest proof is a run
against them, and those runs are the operator's. So this procedure is short, and it ends with work
owed to the operator rather than a green tick. There are two exceptions. The Python beside them:
the root component runs the unit tests in `ansible/roles/dhcp_probe/tests`, `scripts/rotation`,
`scripts`, `support/iac-agent/tests` and `.vscode`. And the openbao role's `tasks/approle.yml`: the
`ansible` component runs `ansible/roles/openbao/tests/approle/run.sh`, which converges that file
against a throwaway dev OpenBao in the `iac` sidecar, normally and with `--check`. A dev server
stands in for the API approle.yml talks to, not for the hosts the rest of the role configures.

## 1. The gates

`kc project test` across every repo the slice touched. In this repo that is yamllint +
ansible-lint over `ansible/`, the approle.yml harness, `terraform fmt -check` over `terraform/`,
the architecture validator, and the root component's unit tests. Red is a finding; route it per the
bar in your dispatch.

The harness starts from an applied server: it applies, re-applies and runs `--check`, each expected
at `changed=0`, then deletes AppRole `rotator` and expects `--check` to pass (slice 045's HTTP 404
reading the role_id of an AppRole not yet created), applies again and runs `--check` once more.

Treat a green gate as what it is: syntax and style, the unit-tested Python's own logic, and
approle.yml's behaviour against a dev server. It says nothing about whether the rest of the roles
converge, whether they are idempotent, or whether they do the right thing. **Never record a
verification item as satisfied on the strength of a green gate alone.**

## 2. Static verification of the things that bite here

Read the diff and check these explicitly, because lint does not:

- **Idempotence.** Does every new or changed task have a natural `changed` signal? A `command` or
  `shell` without `creates:` / `removes:` / `changed_when:` reports changed on every run — a
  finding, not a nit.
- **Check-mode honesty.** Would `--check` produce a meaningful diff for this change, or does it
  silently skip the part that matters? A `command` task skipped under check mode means the
  operator's dry run proves nothing about it. Say so.
- **Blast radius.** Which hosts does the change reach — what `--limit` and which groups? A role
  edit that lands on every host in `site.yml` is a different risk from one scoped to a group.
  State it plainly in your verdict; the operator decides from that.
- **Cluster-serial safety.** Never two k8s or ceph nodes disrupted at once. A change that could
  take a node down needs to say what holds that line — the play's `serial: 1`, or a `throttle: 1`
  on the mutating task itself — and whether the drain/handoff path still holds.

## 3. Read-only live checks

These need no operator gate and are worth running when the slice touched something inspectable:

- `ansible -m setup` / ad-hoc read-only modules against an affected host.
- `ansible-playbook --check --diff` **only** where the role itself has no side effects. If you are
  not certain, do not run it — hand it over instead.
- `terraform state list` / `show` — state reads work from this pod. `plan` does not: the Proxmox
  credentials are not here, so it fails on missing variables. Do not report that failure as a
  finding; it is the environment, not the slice.
- SSH read-only inspection on managed hosts (`qm config`, `lsblk`, file reads).
- **OpenBao role scripts against a throwaway dev server.** `bao` lives in the `iac` sidecar and
  `curl`/`jq` only in the dev container, but both share the pod's network namespace, so
  `cexec iac timeout 900 bao server -dev -dev-root-token-id=root -dev-no-store-token -dev-listen-address=127.0.0.1:18200`
  (backgrounded; the timeout makes it exit even if the kill misses) is reachable from either side.
  Without `-dev-no-store-token` the server writes its root token to `~/.vault-token`, in the home
  every environment shares.
  Seed it with `cexec iac sh -c 'export BAO_ADDR=http://127.0.0.1:18200 BAO_TOKEN=root; bao kv put secret/…'`
  — the dev server's KV-v2 mount is `secret/`, not prd's `kv/` — then run the role's bash with its
  Jinja variables sed-substituted, and stop it with
  `cexec iac pkill -f "server -dev -dev-root-token-id=root"`. Its storage is inmem, not Raft
  (`sys/leader` reports `is_self: false`, no snapshot), so exercise wrapper sections, not a whole
  backup script. approle.yml has its own harness (§ 1); run that rather than building one.

See [live-infra-access.md](live-infra-access.md) for the mechanics.

## 4. Push, and what it now does

Push what the slice committed — the driver checks for it and bails otherwise. What the push does
depends on the repo.

**A push to this repo's `main` converges nothing.** It triggers `IaC/Build-Main`, which runs the lint
gates and `terraform validate`, then `terraform plan` and the protected-VM destroy check, and
stops. That build going green is a real signal and worth recording: it means the commit passes
the gates, would apply cleanly and destroys nothing protected. Wait for it and read it.

Convergence is the separate `IaC/Apply` job. **Do not start it.** That is the operator gate, and
it is the whole reason the pipeline was split.

**A push to an Argo CD deploy repo's `main` deploys.** Every stage that auto-syncs — the default,
prd included — syncs the new commit, and its PreSync hook applies the stage's Terraform at that
commit ([Argo CD runbook](runbooks/argocd.md#registering-undeploying-and-unregistering-an-app)).
The push is the rollout, so whether the run makes it or holds it for the operator is settled in
the plan; follow that. When the run pushes, prove the change live within the run: wait for the
stage's Application to sync and go healthy, read the hook Job's log where the slice changed
Terraform, then take the live readings the plan's criteria name.

## 5. The operator gate — what to hand back

Every slice that changes a role, playbook, inventory or Terraform module in this repo ends
**deploy-owed**.
Close the test phase by writing, in the verdict summary, the exact commands the operator runs:

- Check-mode first, then the apply — same command with the trailing `--check` deleted. Follow the
  canonical shape in [live-infra-access.md](live-infra-access.md).
- Or, where CI is the right route: "push is done and `IaC/Build-Main` is green; start `IaC/Apply`."

Mark the affected `verification.json` items as **owed to the operator**, with the command that
will settle each. Do not mark them verified, and do not let a green gate stand in for a run that
has not happened. If the operator has already applied and reported back within the run, record
their output as the evidence.

A deploy-repo change the run pushed is not deploy-owed: it is live, and the readings of §4 are the
evidence for its items. One whose push the plan held is owed that push: hand back the repo, the
commit, and what to read once its stages have synced.

## 6. Findings

Route per the bar in your dispatch. Two riders specific to this repo:

- **A failure against real infrastructure is never flaky.** If the operator reports a non-zero
  `changed` count where the slice expected idempotence, or a task that failed on one host in a
  group, that is a finding with the operator's output as its evidence.
- **Never work around a missing credential or access path.** If the procedure cannot run because
  something is not reachable from this pod, that is `blocked` — say what was missing.
