# Slice testing strategy

How a slice is proven once its phases are merged. The run loop's test phase is "read this doc and
execute it".

**There is no runnable test suite in this repo, and that is a decision, not a gap.** What Ansible
and Terraform do is converge real machines; the only honest proof is a run against them, and those
runs are the operator's. So this procedure is short, and it ends with work owed to the operator
rather than a green tick.

## 1. The gates

`kc project test` across every repo the slice touched. In this repo that is yamllint +
ansible-lint over `ansible/`, `terraform fmt -check` over `terraform/`, and the architecture
validator. Red is a finding; route it per the bar in your dispatch.

Treat a green gate as what it is: syntax and style. It says nothing about whether the role
converges, whether it is idempotent, or whether it does the right thing. **Never record a
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

See [live-infra-access.md](live-infra-access.md) for the mechanics.

## 4. Push, and what it now does

Push what the slice committed — the driver checks for it and bails otherwise.

**A push to `main` no longer converges anything.** It triggers `iac-on-push`, which runs
`terraform plan` and the protected-VM destroy check and stops. That build going green is a real
signal and worth recording: it means the commit would apply cleanly and destroys nothing
protected. Wait for it and read it.

Convergence is the separate `iac-apply` job. **Do not start it.** That is the operator gate, and
it is the whole reason the pipeline was split.

## 5. The operator gate — what to hand back

Every slice that changes a role, playbook, inventory or Terraform module ends **deploy-owed**.
Close the test phase by writing, in the verdict summary, the exact commands the operator runs:

- Check-mode first, then the apply — same command with the trailing `--check` deleted. Follow the
  canonical shape in [live-infra-access.md](live-infra-access.md).
- Or, where CI is the right route: "push is done and `iac-on-push` is green; start `iac-apply`."

Mark the affected `verification.json` items as **owed to the operator**, with the command that
will settle each. Do not mark them verified, and do not let a green gate stand in for a run that
has not happened. If the operator has already applied and reported back within the run, record
their output as the evidence.

## 6. Findings

Route per the bar in your dispatch. Two riders specific to this repo:

- **A failure against real infrastructure is never flaky.** If the operator reports a non-zero
  `changed` count where the slice expected idempotence, or a task that failed on one host in a
  group, that is a finding with the operator's output as its evidence.
- **Never work around a missing credential or access path.** If the procedure cannot run because
  something is not reachable from this pod, that is `blocked` — say what was missing.
