# Design philosophy

The non-negotiable rules for changing this repo. `CLAUDE.md`'s `Design philosophy:` line points
here; the `code-writer` reads it before touching anything.

## Idempotence is the bar, not a nice-to-have

Every task must be safely re-runnable — a second run of a converged host reports `changed=0`.

- Prefer modules over `command`/`shell`. If you must shell out, add `creates:` / `removes:` or an
  explicit `changed_when:`.
- A task that reports `changed` on every run is a bug, not noise. It makes `--check` output
  unreadable and hides the changes that matter.
- Roles own their concern end-to-end. Role defaults in `defaults/main.yml`, host-specific settings
  in `host_vars/`, environment-level in `group_vars/`.

## Hostnames, not IPs

All managed hosts resolve under the `.home` search domain. Use short hostnames in inventory and
task arguments. Don't hard-code IPs.

## Check-mode first

For any change against real infrastructure, the operator runs `--check --diff` before applying.
Write tasks so check mode is meaningful: a role that can't be dry-run is a role that can't be
reviewed. See [live-infra-access.md](live-infra-access.md) for how the commands are handed over.

## Breaking changes: change it, don't wrap it

This is a single-operator homelab with no external consumers and no deprecation window. When a
variable, role interface, or module input changes shape, **change it and update every caller** —
do not add a compatibility shim, a fallback branch, or a "legacy" code path. Two ways to spell one
thing is how the next reader gets it wrong.

The same goes for tombstones: delete removed code rather than commenting it out. Git remembers.

## No defensive caveats

Don't add error handling for conditions that cannot occur, `ignore_errors:` to quiet a task you
haven't understood, or `failed_when: false` to make a run look clean. A task that fails is telling
you something. If a failure genuinely is acceptable, say why in a comment — that is a non-obvious
*why*, and it earns its place.

## Explanatory notes decay

Files gain scaffolding while they're being built — TODO markers, inline justifications,
walk-through comments, README-style context embedded in role defaults. Once we've moved past a
file, strip the sprinkles. Keep only comments that carry a non-obvious *why*.

Rule of thumb: if the comment exists because we were *building* the file together, delete it when
we move on. If it would help a reader who opens the file in a year knowing nothing of its history,
keep it.

This applies to slice documents in `/work/AnsibleSpecs/` too. Once a slice is done, compress its
document down to what remains operationally useful.

## What "tested" means here

There is no runnable test suite. `kc project test` runs yamllint, ansible-lint and
`terraform fmt -check` — that is a syntax and style gate, and passing it proves nothing about
behaviour. Behaviour is proven by the operator applying the change against real infrastructure and
reporting the output back. Never describe a change as verified on the strength of a green lint.

The full picture is in [slice-testing-strategy.md](slice-testing-strategy.md).

## Commit early and often

Small, focused commits with clear messages. Do not batch unrelated changes into one commit. When
you finish a coherent chunk of work — a role, a runbook update, a decision-record change, a new
playbook — commit it before starting the next. When in doubt, commit.

Commit straight to the working branch (usually `main`) as you go — no topic/feature branches. This
is single-person homelab territory; there's no one to open a PR against, and a branch just adds a
merge step. Same rule in the sibling repos (HelmCharts, DockerImages).

`/work/AnsibleSpecs` is a separate git repo with its own history — commit there too, and stage by
name: it is a shared working tree and parallel sessions live in it.
