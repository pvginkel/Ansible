# 09 — Helm + Terraform deploy harness

## Goal

Reshape the application monorepo (`/work/HelmCharts`) around the
three-tier ownership model so per-release Terraform sits next to its
chart and one CLI orchestrates the deploy. After this plan lands:

- Each release directory carries `values.yaml`, an optional
  `infrastructure.tf` (durable resources the chart depends on — namespace,
  static PVs, Ceph images, ZFS datasets), and an optional
  `configuration.tf` (resources that depend on the chart being reachable
  — Keycloak realm config).
- `poetry run deploy <release> [--stage=<stage>]` runs the right ordering:
  TF infra apply → `helm upgrade --install` → TF config apply.
- The `*.sh` symlink tree retires; `template`, `stop`, `uninstall`,
  `destroy` are subcommands of the same CLI.
- Namespaces become TF resources, with `<chart>-<stage>` naming
  including prd — the prd-no-suffix asymmetry is gone.

This plan is the substrate for phase 8 (storage migration to TF) and
phase 9 (Keycloak realms/clients/roles to TF). It runs after plan 08
lands the provider resource types those phases need.

## Decisions taken with the operator

- **Stage is part of every invocation.** `--stage=prd` is the default;
  every namespace carries the stage suffix.
- **Helm stays the runtime tool.** No `helm_release` TF wrapping. TF
  and Helm run side-by-side under the CLI.
- **Per-release TF state**, file-based on a state Git repo per
  decisions.md "Production execution model." Releases that share
  cross-stage TF (e.g. a Keycloak realm consumed by all four stages)
  use a sibling `_shared/` directory.
- **Static PVs with `claimRef` + `Retain`** — declarative naming,
  lifecycle decoupled from any one Helm release.
- **`uninstall` and `destroy` are separate commands.** A redeploy
  after `uninstall` reuses the still-existing TF resources via the
  static-PV `claimRef` discipline.
- **ZFS pools by Ansible, datasets by TF.** Pool creation is per-host,
  lifetime equals the VM's. Datasets are per-release.
- **Operator-runs-TF carve-out.** The existing rule in CLAUDE.md
  governs this repo's `terraform/`. The application monorepo's
  per-release TF runs through Jenkins like Helm always has.

## Repository layout

```
/work/HelmCharts
├── charts/                         # unchanged — Helm chart sources
├── configs/
│   ├── dev/                        # chart-development against wrkdevk8s
│   │   └── <chart>/
│   │       ├── release.yaml
│   │       ├── values.yaml
│   │       └── infrastructure.tf   # optional
│   └── prd/                        # production cluster
│       └── <chart>/
│           ├── _shared/            # cross-stage TF (e.g. realm)
│           │   ├── shared.tf
│           │   └── …
│           ├── prd/                # always present, even for stageless charts
│           │   ├── release.yaml
│           │   ├── values.yaml
│           │   ├── infrastructure.tf
│           │   └── configuration.tf
│           ├── uat/
│           │   └── …
│           └── tst/
│               └── …
├── terraform-modules/              # reusable TF modules
│   ├── namespace/
│   ├── static-rbd-pv/
│   ├── static-cephfs-pv/
│   └── static-zfs-pv/
├── _providers/                     # shared provider config
│   └── providers.tf
├── tools/
│   └── deploy/                     # Python CLI
│       └── pyproject.toml
└── Jenkinsfile                     # invokes the CLI
```

**Stage is always required, including for charts that have only one
deployment.** Stageless charts (`dnsmasq`, `registry`, etc.) live at
`configs/prd/<chart>/prd/{release.yaml, values.yaml,
infrastructure.tf}` — the default stage is `prd`, and there's no
conditional layout collapse. One path-resolution rule everywhere; the
small cost is a `prd/<chart>/prd/` visual redundancy on stageless
charts, which is a worthwhile trade for a uniform CLI.

The `dev` config tree (chart development against `wrkdevk8s`) follows
the same shape: `configs/dev/<chart>/<stage>/`. Most dev configs use a
single stage; conventionally that's `dev` (so
`configs/dev/<chart>/dev/`), but the harness doesn't constrain it.

## `release.yaml` schema

```yaml
chart: design-assistant       # path under charts/
namespace: design-assistant   # base name; CLI appends -<stage>
phases:
  infra: true                 # apply infrastructure.tf before helm
  config: false               # apply configuration.tf after helm
helm_args: []                 # extra helm flags if needed
```

Most fields default to convention. `release.yaml` is required only
where the release diverges from the convention (different chart name
than directory, custom helm flags, etc.).

## CLI

Entry point: `poetry run deploy` (Python, lives in `tools/deploy`).
Subcommands:

| Command       | Effect                                                                      |
|---------------|-----------------------------------------------------------------------------|
| `deploy`      | TF infra apply → `helm upgrade --install` → TF config apply.                |
| `template`    | `helm template` only. No apply, no TF.                                      |
| `stop`        | Scale every workload in the release's namespace to zero replicas.           |
| `uninstall`   | `helm uninstall` only. TF resources stay. Redeploy reattaches cleanly.      |
| `destroy`     | TF destroy of `configuration.tf` then `infrastructure.tf`.                  |

Invocation: `poetry run deploy <release> [--stage=<stage>]`.

`<release>` is the path under `configs/`, e.g. `prd/design-assistant`
or `dev/design-assistant`. With `--stage=uat`, the full filesystem
path resolves to `configs/prd/design-assistant/uat/`.

`uninstall` and `destroy` separate so the operator can:

- Take an app down for migration without removing data (`uninstall`,
  then re-`deploy` later — the PV's `claimRef` catches the recreated
  PVC).
- Decommission a release (`uninstall` → `destroy`).

`destroy` refuses while any TF resource has `prevent_destroy = true`
set. Removing the flag is a deliberate two-step the operator commits
to git before the destroy lands.

## State, providers, secrets

- **State backend.** File-based, stored in the state Git repo per
  decisions.md "Production execution model." One state file per
  `(release, stage, phase)` triple — `infra` and `config` are
  separate states.
- **Providers.** `_providers/providers.tf` declares `homelab`
  (extended per plan 08), `kubernetes`, `keycloak`. Each release
  pulls it via symlink or include — settled at impl time. Symlink
  wins on maintenance; the alternative is per-release duplication
  with a `terraform fmt`-driven sync check.
- **Credentials.** From OpenBao via Jenkins credential injection,
  exposed to the deploy container as environment variables. The CLI
  passes them to TF via `TF_VAR_*` and to Helm via `--set` /
  `--set-file` as needed. **Pre-phase 6 stopgap**: a gitignored
  `credentials/` directory on the deploy host holds the same
  variables; the CLI reads from it when the env vars aren't set. The
  stopgap commits explicitly to migrating to OpenBao when phase 6
  lands.

## Namespace migration

Today every prd release's namespace is `<chart>` (no suffix). New
convention is `<chart>-prd`. Per-chart migration (one chart at a time,
no flag day):

1. Create the new TF-owned namespace (`<chart>-prd`) via the new
   `infrastructure.tf`.
2. Stop scheduling new resources in the old namespace.
3. For every PV bound to a PVC in the old namespace: update the PV's
   `claimRef.namespace` to the new namespace — TF resource change
   plus `terraform apply`. This is one of the migration paths plan 08's
   resource types must support cleanly.
4. Cut the workload over: `helm upgrade --install` against the new
   namespace.
5. After soak, `kubectl delete ns <old>`.

For charts whose namespaces are already stage-suffixed (anything
already deployed via `<chart>@<stage>` to a `<chart>-<stage>`
namespace), there's nothing to migrate.

## Verification

Pick one chart with PVCs as the proof. `design-assistant` is the
candidate — already multi-stage, already on Ceph. Drive end-to-end:

1. Restructure to `configs/prd/design-assistant/{prd,uat,tst}/`.
2. `infrastructure.tf` per stage creates namespace + RBD image +
   static PV.
3. Helm chart updated to use static PVCs (per-PV `volumeName` fields
   in values.yaml).
4. `poetry run deploy prd/design-assistant --stage=uat` is end-to-end
   green and reproduces the previous deploy state.
5. `poetry run uninstall prd/design-assistant --stage=uat` removes
   the Helm release; PVs and Ceph images remain.
6. `poetry run deploy prd/design-assistant --stage=uat` reattaches
   the existing PVs cleanly.
7. `poetry run destroy prd/design-assistant --stage=uat` refuses while
   `prevent_destroy = true` is set; succeeds on a throwaway test stage
   after the flag is lifted.

Once design-assistant works, phase 8 is mechanical per-chart migration.

## Caveats

- **Provider config in modules vs root.** TF allows provider blocks in
  child modules but discourages it. The `_providers/` directory will
  end up symlinked or included in each release root; symlink is uglier
  on disk, cleaner to maintain. Decide at impl time.
- **Two TF states per release.** `infra` and `config` are separate
  states, which means two `terraform init` calls and two state files
  per release-stage. Slight overhead; acceptable given the lifecycle
  separation.
- **`stop` is not sticky.** `stop` scales the current set of workloads
  to zero. A subsequent `deploy` brings them back at the chart's
  default replicas. Good — `stop` is a maintenance tool, not a
  feature flag.
- **Phase ordering is enforced by the CLI**, not by TF or Helm. A
  failed `infrastructure.tf apply` aborts before Helm runs; a failed
  Helm upgrade aborts before `configuration.tf` runs. Recovery is
  manual — re-run the CLI after fixing the cause; the CLI is
  idempotent end-to-end.

## Commits

1. This plan, here.
2. (HelmCharts repo) `tools/deploy` CLI scaffolding.
3. (HelmCharts repo) `terraform-modules/` and `_providers/`
   scaffolding.
4. (HelmCharts repo) one chart migrated end-to-end as the proof.
5. (HelmCharts repo) per-chart migration commits, mechanical, one per
   chart.
