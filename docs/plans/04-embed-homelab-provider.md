# 04 — Embed the homelab provider in the `modern-app-dev` image

## Goal

Cut the `pvginkel/homelab` provider over from the local dev override to a
binary embedded in the `modern-app-dev` Docker image. After this plan
lands, `terraform init` resolves the provider from a baked-in filesystem
mirror with no per-machine setup — same path for Claude, the Jenkins CI
build, and the operator's manual `terraform apply` runs (all of which
happen inside `modern-app-dev`).

This plan runs **after** plan 02. Plan 02 wires the provider into the
per-VM module against the dev override, so we can iterate on the
provider quickly while shaking out bugs in the wiring. Once plan 02 is
stable on the dev override, plan 04 swaps the install path underneath
it.

Decisions taken with the operator:

- Provider source repo:
  [`pvginkel/HomelabTerraformProvider`](https://github.com/pvginkel/HomelabTerraformProvider)
  on GitHub.
- Operator owns the Jenkins pipeline end-to-end (provider build job,
  artefact archive, `modern-app-dev` cascading trigger). This plan
  specifies the **container-side contract** — where the binary lands,
  what `terraformrc` config goes alongside it, how the version is
  named — and leaves the pipeline mechanics to the operator.
- Build artefact: linux/amd64 binary archived as a Jenkins artefact;
  pulled into the `modern-app-dev` Kaniko build context via
  `copyArtifacts`. No external HTTP host, no GitHub release, no
  registry publication.
- Cascading rebuild: provider job kicks the `modern-app-dev` build by
  job name on success.
- Version model: the container image is the source of truth. Terraform
  source pins the provider with a loose constraint (or none); whichever
  binary the current image ships is what gets used.
- Single arch: linux/amd64 only. arm64 is not in scope.
- All operator `terraform apply` runs happen inside `modern-app-dev`,
  not from bare metal — so no operator-workstation install path is
  needed.

## Steps

### Provider repo build

Operator-owned. The contract this plan needs from it:

- Jenkins job builds `terraform-provider-homelab_v${VERSION}` for
  `GOOS=linux GOARCH=amd64` and archives it.
- `VERSION` is the git tag (`v0.1.0` → `0.1.0`) on tagged builds, or
  `0.0.0-${SHORT_SHA}` otherwise. Whatever string is chosen, it must
  match the path the Dockerfile lays out.
- On success, triggers the `modern-app-dev` Docker build job by name.

### `modern-app-dev` image

Lives in a separate repo (operator confirms the exact path — likely
`/work/DockerImages`). The image needs three additions:

**Jenkinsfile** — before the Kaniko stage, `copyArtifacts` the latest
successful provider binary from the upstream job into the build
context. Resolve the version string and pass it as a Docker build arg.

**Dockerfile** —

```dockerfile
ARG HOMELAB_PROVIDER_VERSION
COPY terraform-provider-homelab_v${HOMELAB_PROVIDER_VERSION} \
  /usr/local/share/terraform/plugins/registry.terraform.io/pvginkel/homelab/${HOMELAB_PROVIDER_VERSION}/linux_amd64/terraform-provider-homelab_v${HOMELAB_PROVIDER_VERSION}
RUN chmod +x /usr/local/share/terraform/plugins/registry.terraform.io/pvginkel/homelab/${HOMELAB_PROVIDER_VERSION}/linux_amd64/terraform-provider-homelab_v${HOMELAB_PROVIDER_VERSION}

COPY terraformrc /etc/terraformrc
ENV TF_CLI_CONFIG_FILE=/etc/terraformrc
```

**`terraformrc`** (new file in the image build context):

```hcl
provider_installation {
  filesystem_mirror {
    path    = "/usr/local/share/terraform/plugins"
    include = ["registry.terraform.io/pvginkel/homelab"]
  }
  direct {
    exclude = ["registry.terraform.io/pvginkel/homelab"]
  }
}
```

The `direct { exclude = … }` block stops Terraform falling back to the
public registry for the homelab provider when the mirror lookup fails
— without it, a missing binary turns into a confusing "404 from
registry.terraform.io" instead of a useful error.

The `registry.terraform.io/pvginkel/homelab` namespace in the mirror
path is just an identifier — it matches the source address Terraform
resolves to internally for `pvginkel/homelab`, even though we never
publish there. The mirror is consulted first; the namespace is
metadata, not a network call.

### Cutover

Performed once plan 02 is verified working against the dev override and
the `modern-app-dev` image with the embedded binary has rolled out.

- `docs/runbooks/operator-workstation.md`: remove the `~/.terraformrc`
  dev-override line that plan 02 added. The image's
  `TF_CLI_CONFIG_FILE=/etc/terraformrc` takes over; nothing
  per-workstation is needed.
- `terraform/prd/versions.tf` and `terraform/scratch/versions.tf`:
  leave the `homelab = { source = "pvginkel/homelab" }` block as plan
  02 wrote it. No version constraint — container image is the version
  of truth.
- In a fresh `modern-app-dev` container, with the operator's
  `~/.terraformrc` either gone or with the dev-override removed: run
  `terraform init -upgrade` from `terraform/scratch`. Confirm the
  install path is the filesystem mirror, not the dev override (see
  Verification).
- Once verified, the dev override on the operator's workstation can be
  deleted.

## Verification

- In a freshly built `modern-app-dev` container, with no
  `~/.terraformrc` and no `TF_CLI_CONFIG_FILE` override on the command
  line: `cd terraform/scratch && terraform init` resolves
  `pvginkel/homelab` and reports "Installed pvginkel/homelab v<X>
  (from /usr/local/share/terraform/plugins/…)". Critically, the path
  in that line is the mirror's, not `/work/HomelabTerraformProvider`.
- `terraform providers` lists
  `registry.terraform.io/pvginkel/homelab v<X>`.
- Re-run `terraform init` with `TF_LOG=DEBUG` and confirm there is no
  network attempt to `registry.terraform.io` for the homelab provider
  — i.e. the `direct { exclude }` block is doing its job.
- Push a no-op commit to the provider repo; confirm Jenkins builds it,
  archives the binary, and the `modern-app-dev` job kicks
  automatically.
- After the new image rolls out, `terraform init -upgrade` from
  `terraform/scratch` reports the new version and `terraform plan` is
  no-op (no resource diffs from a provider bump).

## Caveats

- linux/amd64 only. If a Jenkins agent or operator workstation ever
  needs a different arch, the provider Jenkinsfile needs a build
  matrix and the Dockerfile needs a parallel `COPY` for the second
  arch into the matching `<os>_<arch>` directory under the same
  version path.
- The container image is the version pin. If a `terraform apply` is
  ever run from a stale image, it'll use the stale provider — there
  is no second check. Acceptable trade-off given the homelab-scale
  fleet and that all `terraform apply` paths run inside the same
  image.
- The dev override on the operator's workstation must be removed at
  cutover, not just superseded. With both a dev override and a
  filesystem mirror in play, dev override wins silently — meaning the
  cutover would look successful without actually being in effect.

## Commits

1. This plan, here in `docs/plans/04-embed-homelab-provider.md`.
2. (Operator-owned, separate repos) provider Jenkinsfile;
   `modern-app-dev` Jenkinsfile + Dockerfile + `terraformrc`.
3. Cutover edits in this repo: `docs/runbooks/operator-workstation.md`
   removes the dev-override line. One commit, after verification.
