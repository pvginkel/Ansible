# step-ca root rotation runbook

The homelab CA's **root** — the trust anchor every managed host, image and chart
carries — as opposed to the intermediate, which
[`step-ca-bootstrap.md`](step-ca-bootstrap.md) covers. Rotation is planned as a
single event in year 9 of the root's 10-year validity; the transition needs the
outgoing and incoming roots trusted simultaneously, so it is a fleet-wide change
window rather than a command.

- [Status — what this runbook does not yet contain](#status--what-this-runbook-does-not-yet-contain)
- [The trust anchor and every copy of it](#the-trust-anchor-and-every-copy-of-it)
- [What a rotation breaks besides TLS: the provider mirror](#what-a-rotation-breaks-besides-tls-the-provider-mirror)
- [The one-change-window rule](#the-one-change-window-rule)
- [Verifying the inventory is still whole](#verifying-the-inventory-is-still-whole)

Design context is the "Root rotation mechanism" section of
[`/work/AnsibleSpecs/decisions.md`](../../../AnsibleSpecs/decisions.md). This
runbook is the *operational* path; it doesn't re-justify decisions.

---

## Status — what this runbook does not yet contain

**The step-by-step cutover procedure is still owed.** It is not written here
because the estate cannot execute it yet: two of the three mechanisms
`decisions.md` lists as gating the next rotation are unimplemented, and the
procedure's shape depends on how they land.

- **`baseline` installs exactly one root.** `roles/baseline/tasks/main.yml`
  copies `files/homelab-root.crt` to
  `/usr/local/share/ca-certificates/homelab-root.crt` as a single file. Debian's
  `update-ca-certificates` processes only the first PEM block per `.crt`, so a
  two-root bundle dropped there is silently truncated — the second root would
  never reach a host's trust store. The bundle-splitting, fingerprint-named,
  reconciling task `decisions.md` specifies does not exist.
- **The drift check is a byte diff.** The `Homelab CA root drift` stage in
  [`Jenkinsfile.iac-scheduled-drift`](../../Jenkinsfile.iac-scheduled-drift)
  `diff -u`s the in-repo cert against `https://ca.home/roots.pem`. The moment
  the bundle carries two roots the two sides' ordering is no longer pinned and
  the stage fires on every run. It needs the fingerprint-set comparison
  `decisions.md` specifies first.
- **The deduplication decision is open.** Six out-of-repo copies of the root
  are maintained by hand (below). Whether they collapse to one source or stay
  copies changes what a rotation's change window contains.

**This wants a slice**, not a doc pass: two of the three are code changes to a
role and a pipeline. Until it runs, treat the sections below as the part of the
rotation that *is* settled — the inventory of what a root change has to move,
and the order it has to move in. Do not improvise the cutover from them.

## The trust anchor and every copy of it

The canonical copy — the one to edit first, and the one every other is a
duplicate of:

```
/work/Ansible/ansible/roles/baseline/files/homelab-root.crt
```

It is public, PEM-armored, and committed. `baseline` distributes it to every
managed host, and the step-ca bootstrap ceremony exports it here (step 6 of
[`step-ca-bootstrap.md`](step-ca-bootstrap.md)).

**Six out-of-repo copies are on this inventory**, all byte-identical to it, and
a rotation updates all six.

| Path | What consumes it |
|---|---|
| `/work/HelmCharts/homelab-root.crt` | The `external-secrets` chart's `post-rollout.sh` builds a `ca.crt` Secret from it. |
| `/work/HelmCharts/charts/nginx/files/ca/homelab-root.crt` | Mounted by the nginx manager Deployment and its renewal CronJob; the `certbot` image's `args.sh` bind-mounts this same file at run time. |
| `/work/ArgoCDTools/image/homelab-root.crt` | Baked into the `argocd-hook` image's trust store — the Argo CD Terraform PreSync hook. |
| `/work/DockerImages/kube-coder-dev-base/homelab-root.crt` | Baked into the KubeCoder dev base image's trust store, and pointed at by `NODE_EXTRA_CA_CERTS`. |
| `/work/KubeCoder/controller/homelab-root.crt` | Baked into the KubeCoder controller image's trust store — how the controller validates `https://ca.home` when it asks step-ca to sign an environment pod's SSH host certificate. |
| `/work/ArgoCDDeploy/chart/files/homelab-root.crt` | Rendered into a ConfigMap and mounted into Argo CD's repo-server at `/etc/ssl/certs/homelab-root.crt`, which is how `helm dependency build` comes to trust `https://charts.home`. Not an image copy: it lands on Argo's next sync of its own chart, which is manual (D3). |

**Two images consume the cert without holding their own copy** — they need no
edit, but they do need a rebuild:

- `support/iac-image/Dockerfile` COPYs the canonical file straight out of the
  Ansible repo into the `iac` image's trust store. Built by
  `Jenkinsfile.iac-image`.
- Every image descended from `kube-coder-dev-base`. The `DockerImages` job
  rebuilds an image directory's descendants along with it, so a push that edits
  the copy above covers them.

Editing a file in an image's build context is not the same as the change
landing: the `argocd-hook`, `iac`, `kube-coder-dev-base` and KubeCoder
controller copies only take effect once their image is rebuilt **and** the
workloads pulling it are restarted onto the new tag. The `ArgoCDDeploy` copy
needs no rebuild, but it does need a sync — and Argo CD syncs itself only when
the operator says so, so it is the one copy a rotation can leave behind without
any pipeline noticing.

## What a rotation breaks besides TLS: the provider mirror

`https://tfmirror.home/` serves a leaf signed by this root, and it is the only
source of the `pvginkel/homelab` Terraform provider. An image whose trust store
has not caught up with a root rotation fails at `terraform init` — it cannot
verify the mirror at all — and every deploy repo's Terraform declares that
provider.

The routing lives in `/etc/terraform.rc`, at `TF_CLI_CONFIG_FILE`, and there are
**four byte-identical copies** of that file too. They matter here for the same
reason the cert copies do: the same images are on both lists, so a rotation and
a mirror readdressing touch the same rebuild set.

- `/work/Ansible/support/iac-image/terraform.rc` — the `iac` image
- `/work/DockerImages/modern-app-dev/terraform.rc` — the dev container
- `/work/DockerImages/kube-coder-dev-base/terraform.rc` — the KubeCoder dev base image
- `/work/ArgoCDTools/image/terraform.rc` — the Argo CD PreSync hook image

Readdressing `tfmirror.home` on its own — no root change — is the same edit
without the cert half; see the "Terraform `pvginkel/homelab` provider" section
of [`operator-workstation.md`](operator-workstation.md).

## The one-change-window rule

Everything in the inventory above moves in a single change window, and the
window has an ordering constraint:

**The repos land the new root *before* it starts appearing in step-ca's
`/roots.pem`.** The drift stage compares the in-repo cert against the live CA
and pins its own TLS handshake to the in-repo copy, so a CA that serves a root
the repos do not carry both fails the comparison and can fail the fetch. Adding
first and publishing second keeps drift quiet through the transition.

Removing the outgoing root is the mirror image, and comes only after the fleet
has demonstrably picked up the incoming one: retire it from step-ca last, and
from the repos and hosts after that.

## Verifying the inventory is still whole

The seven paths are duplicates by convention, not by mechanism, so drift
between them is silent. Check them against each other before and after any
change window:

```sh
md5sum /work/Ansible/ansible/roles/baseline/files/homelab-root.crt \
       /work/HelmCharts/homelab-root.crt \
       /work/HelmCharts/charts/nginx/files/ca/homelab-root.crt \
       /work/ArgoCDTools/image/homelab-root.crt \
       /work/DockerImages/kube-coder-dev-base/homelab-root.crt \
       /work/KubeCoder/controller/homelab-root.crt \
       /work/ArgoCDDeploy/chart/files/homelab-root.crt
```

All seven hashes must match.

The same check for the provider mirror config:

```sh
md5sum /work/Ansible/support/iac-image/terraform.rc \
       /work/DockerImages/modern-app-dev/terraform.rc \
       /work/DockerImages/kube-coder-dev-base/terraform.rc \
       /work/ArgoCDTools/image/terraform.rc
```

And the live CA against the repo, which is what the scheduled drift stage does
on its own cadence:

```sh
root_crt=/work/Ansible/ansible/roles/baseline/files/homelab-root.crt
curl --cacert "$root_crt" --silent --fail https://ca.home/roots.pem | diff -u "$root_crt" -
```
