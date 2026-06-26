# Triage

Turn a batch of findings, requests, and issue-tracker items into grounded **change-request
bundles** — self-contained work packages the slice writer picks up later. Argument (optional):
path to a findings document (e.g., `../AnsibleSpecs/handovers/some_findings.md`).

The input can be a list of observations from a real run, a list of bugs, a change-request dump,
chat discussion, or any unstructured collection of issues. Triage understands it, groups it by
subject, and writes one bundle per group. **Triage does not write slices** — that is the slice
writer's job, in a separate, deliberate act.

## Triage is for slice-worthy change requests

This repo has a dual role (see `CLAUDE.md`): most infrastructure work — a quick fix, a diagnosis,
an investigation, a one-off operator command, a runbook/doc touch — the orchestrator does **directly,
without a slice**. Triage is for the other path: a managed change to one of the infra repos
substantial enough to warrant a tracked slice. Only that kind of work gets bundled here.

## Triage is mandatory — and it stops at the bundle

- **Every change that will become a slice goes through triage first.** This is a hard rule. The
  bundle is the required input to `/write-slice`; there is no slicing without one.
- **Triage does not start `/write-slice` itself.** When the bundles are written, stop. The
  operator picks a bundle up later — usually in a fresh session — and runs `/write-slice` then.
  Do not assume the operator will action every bundle immediately.
- **The one exception is operator-initiated — never your own judgement.** Only when the **operator
  explicitly asks** you to carry straight on into `/write-slice` in the same session may you do so,
  and only if you also agree the request is a single isolated, genuinely minimal change (a clear bug
  fix, a cosmetic fix, an impactful-but-honestly-straightforward change). You never decide this on
  your own: absent an explicit operator request, you stop at the bundle, full stop. If the operator
  asks but you judge the change is not minimal, say so. Even when you do proceed, the bundle is still
  produced, the full slice-writing process still applies, and you **never** do this from a sub-agent.

## What this skill does

You are the orchestrator. You do not write the infra change, and you do not design the
implementation — you produce work packages that the slice writer (and, downstream, the dev
agents) execute.

## Procedure

### Phase 1: Collect and consolidate

**1a. Gather every input.** Read the findings document if one was passed. Pull in the relevant
chat discussion. Fetch the outstanding `Ansible`-tagged cards in the **Triage board's
Inbox** list (https://trello.com/b/ETTRJ8iW/triage) — the project's intake queue. Leave cards
tagged for other projects alone, and treat untagged cards as not-yet-claimed; if asked to consider a
card without the `Ansible` tag, say so rather than adopting it. All three are inputs and are
considered together.

**1b. Write a transient triage working document** at `../AnsibleSpecs/handovers/triage_YYYY-MM-DD.md`
(transient docs live in `handovers/`, never at the specs root). Give every item a numbered entry
with:

- A clear description of the issue.
- Its source (findings-document reference, issue-tracker id, or both).

This document is scratch — it exists to drive the clarification loop and is **deleted at the end
of triage** (Phase 7), once all information has been absorbed into the bundles.

**1c. Clarify until you fully understand every item.** For every item that is vague, missing
information, or that simply needs research before it can be understood, add a **QUESTION** marker.
Present the document to the operator and iterate until every item is understood. Understanding the
request fully is the whole point of this phase — do not guess.

**Do not group items into bundles yet.** Phase 1 is about understanding individual items, not
deciding how they cluster.

### Phase 2: Ground enough to understand

Research items only to the depth needed to *understand* them — not to design or implement them.
The deep, file:line grounding that briefs depend on is the **slice writer's** job, not triage's.

- For an item whose meaning or feasibility is unclear, read the relevant code (use `Explore`
  sub-agents in parallel for groups of related items) until you understand what is actually being
  asked and whether it is coherent. Code lives across the infra repos — `ansible/`, `terraform/`,
  and the sibling repos (`/work/HelmCharts`, `/work/DockerImages`, `/work/IaCAgent`).
- Record findings back into the triage working document, and raise follow-up **QUESTION** markers
  where the code contradicts the reported behaviour or the request is ambiguous.
- If an item genuinely required research to understand, capture that research as a separate
  document — it becomes an attachment in the item's bundle (Phase 5).

A concrete goal of this phase is to gather enough information that you can group the items **with
confidence** in Phase 4 — you have to understand what each item really is before you can judge what
it belongs with. If you cannot yet tell where an item clusters, you do not understand it well enough.

Iterate follow-up questions with the operator until resolved or explicitly deferred.

### Phase 3: Separate non-actionable items

Identify items that should not become slice work:

- **Already implemented, or a duplicate** → **archive the card** (leave a short comment saying why).
  It never reaches a bundle.
- **Pure discussion / no actionable work** → flag for the operator.
- **Work the orchestrator handles directly without a slice** — a quick fix, a diagnosis, an
  investigation, a one-off operator command, a runbook/doc touch (per `CLAUDE.md`'s dual role) →
  note it for the operator; it is done directly, not bundled.

Present the separation to the operator for confirmation before grouping.

### Phase 4: Group into logical categories

Group the remaining items into **change requests**. Follow these rules:

- **Group by related subject**, not by slice boundaries. You are deciding what work is *about the
  same thing*, not authoring slices. The slice writer decides slice boundaries later.
- **Do not use the number of repos or areas touched as a grouping metric.** Delivering a change
  end-to-end and correct matters more than limiting development complexity — a coordinated
  cross-repo change is still one subject.
- **Favor larger groups.** It is easier for the slice writer to split one bundle into two slices
  than to notice adjacent work that was scattered across separate bundles and never came into
  view. When in doubt, group together.
- **Sanity-check against under-grouping.** Before finalizing, glance at the areas/files each item
  touches (from Phase 2's grounding): items that hit the same role, module, host, or repo almost
  certainly belong together. Over-grouping is cheap for the slice writer to split; *scattering*
  related work across separate bundles is the expensive miss — that is the one to catch here.

### Phase 5: Write the change-request bundles

For each group, create a bundle under `../AnsibleSpecs/change_requests/<snake_case_slug>/`. The
`change_requests/` folder sits next to `slices/`; bundles persist there until the operator actions
them (do not assume immediate action).

Each bundle contains:

- **`change_request.md`** — a self-contained write-up of the change request. It must absorb **all**
  the relevant material so the slice writer can work from the bundle alone:
  - A one-line summary, then the detail of what is being requested and why.
  - **Abstracts of every referenced artifact** — the relevant content of findings-document
    sections, issue-tracker cards, and chat discussion, pulled in (not just linked).
  - The **Q&A** you did with the operator during clarification, captured so the slice writer
    inherits that understanding.
  - **References to the issue-tracker items** that belong to this change request (by id).
- **Attachments (optional)** — any research document you produced in Phase 2, and any pre-existing
  prior work that informs the request. If a relevant document already lives in `handovers/` (a
  prior design or proposal), **move it into the bundle** so the slice writer has it in one place.

Your job here is to *bundle and absorb existing material* into a form the slice writer can focus
on — not to invent design, write acceptance criteria, or propose an implementation.

### Phase 6: Update the issue tracker

The Triage cards are just collected thoughts and ideas — they have no standalone value once a slice
forms. For each `Ansible` card that was folded into a bundle:

- Keep the `Ansible` owner tag and nothing else — the model drops type and area labels.
- Rewrite the card description only where it is too thin to recognise later — enough that the bundle
  stands on its own. Don't over-format; these cards are short-lived.
- Move the card from **Inbox** to **Accepted** — grouped into a change request, not yet sliced.

These are temporary **source cards**: when `/write-slice` forms a slice from the bundle it
**archives the source cards and opens a single card on the Kanban board (To Do) that represents the
slice** (which then flows **To Do → In Progress → Done**). The only cards you archive yourself here
are the already-implemented / duplicate ones separated out in Phase 3 — never the ones bound for a
bundle. Items the operator wants parked rather than sliced go to **Later**; ones rejected outright go
to **Won't Do**.

### Phase 7: Finish

**7a. Delete the transient triage working document.** All information must by now live in the
bundles — the operator should be able to delete the triage scratch doc with nothing lost. If
anything would be lost, it has not been absorbed yet; fix that before deleting.

**7b. Notify the operator:**

```bash
python3 tools/ai_workflow/send_message.py --title "Triage complete" "N items triaged into M change-request bundles under ../AnsibleSpecs/change_requests/. Run /write-slice on a bundle when ready."
```

Then stop. Do not start `/write-slice` (except under the narrow interactive minimal-change
exception at the top of this skill).

## Key principles

- **Ground only to understand.** Read enough code to understand and de-risk each item. Leave the
  deep file:line grounding to the slice writer — doing it twice wastes effort and goes stale.
- **Don't write slices, don't design.** Triage's output is grouped, absorbed work packages. No
  acceptance criteria, no briefs, no interface contracts, no implementation proposals.
- **Favor larger groups.** Splitting later is cheap; missing adjacent work is expensive.
- **Iterate with the operator.** Ambiguous items get a QUESTION and a conversation, not a guess.
- **All information must have a home in the bundle.** When triage is done, the triage working
  document is deleted and every fact lives in a `change_request.md` or its attachments.
- **Don't make slice-worthy changes ad hoc.** If the operator asks for a slice-worthy infra change,
  push back and route it through a bundle (or, if it truly qualifies, the interactive minimal-change
  exception). Non-slice infra work is the orchestrator's to do directly — that is a different path,
  not a triage input.
