# Proxmox credentials for Terraform

Terraform's bpg/proxmox provider authenticates as `root@pam` with username + password. PVE restricts a few config fields (VM `affinity`, arbitrary-path passthrough disks) to root, and the scoped API-token approach we tried earlier could not write either — even tokens derived from `root@pam` are rejected because PVE distinguishes "user logged in" from "token of that user." See `/work/AnsibleSpecs/decisions.md` "Proxmox VM CPU affinity" and "Disk passthrough on managed VMs".

`root@pam` has no MFA on this cluster, so direct password auth works without ceremony.

## Where the password lives

- **OpenBao, `kv/iac/proxmox#password`** — the source, which SecretRotator's `pve-root-password`
  kind rotates ([§ Rotation](#rotation)).
  - On srviac, `/etc/iac/secrets.yaml` resolves it into `TF_VAR_proxmox_password` at every `iac`
    start, so the `IaC/*` jobs and `iac -c` take the current value.
  - In KubeCoder environments, the catalog copy `proxmox-password` (in
    `kv/eso/prd/kubecoder/prd/catalog`) is `TF_VAR_proxmox_password` (`.kubecoder/config.yaml`), so
    `cexec iac terraform …` needs no tfvars file. An environment takes a new value when it restarts.
- **The PVE nodes** — `root@pam` is the Linux `root` account of `pve`, `pve1` and `pve2`, with the
  same password on each.
- **Roboform** — the operator's copy, in the entry `PVE root@pam`. The rotation shows the new
  password to store there.
- **`terraform/{prd,scratch}/terraform.tfvars`** — only on a workstation that runs Terraform outside
  srviac and KubeCoder (break-glass), filled in from Roboform. The file is gitignored (`*.tfvars` in
  `.gitignore`), and a rotation leaves it stale.

## First-time setup on a fresh checkout

```sh
cd terraform/prd      # or terraform/scratch
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars
```

Fill in `proxmox_password` from Roboform. The `proxmox_username` default (`root@pam`) is correct.

Verify:

```sh
terraform init
terraform plan
```

A clean plan against an existing prd state confirms the credentials work.

## Rotation

SecretRotator's `pve-root-password` plan of `iac/proxmox` rotates the password, at 365 days, from
`secret-rotator ui` ([`openbao.md`](openbao.md) §5). Start the UI with
`ssh -t ansible@srviac secret-rotator-ui`. The plan has an operator step, so the nightly run never
starts it; once `pve-root-password` is in the nightly run's `kinds_enabled`, the run announces when
it falls due. The plan:

1. Generates a new password.
2. Sets it as `root`'s password on `pve`, `pve1` and `pve2`, one node after the other
   (`ssh.set_password`). It reaches each node over SSH from srviac's `iac` container as `ansible`,
   checks the host key against the homelab SSH host CA, and runs `sudo chpasswd` with the password
   on stdin.
3. Writes `kv/iac/proxmox#password` and its catalog copy `proxmox-password`, syncs
   `kubecoder-prd/kubecoder-secret-catalog` and restarts `kubecoder-prd/deployment/kubecoder-controller`.
   The controller's restart restarts every prd KubeCoder environment, the one you work from among
   them. The UI keeps running in srviac's tmux session: run `secret-rotator-ui` again to reattach.
4. Shows the new password: store it in Roboform (`PVE root@pam`), then press **Done**.
5. Stamps the key.

A step that fails stops the plan where it failed, with Retry and Abort. Up to that Done, Abort
leaves every node with its old password: it rolls back the copy and the leaf, then sets the old
password, read from OpenBao, back on each node the plan changed, in reverse order, and restarts the
controller again. A node the plan could not reach is left as it was.

srviac's Terraform takes the new password at its next `iac` start. A workstation's
`terraform.tfvars` is updated by hand, from Roboform.

## Leak response

If `terraform.tfvars` leaks (for example, accidentally committed despite the `.gitignore` rule), rotate the password at once by [§ Rotation](#rotation): `secret-rotator ui` lists the plan whether it is due or not. Then update any `terraform.tfvars` left on a workstation.

The `*.tfvars` `.gitignore` rule has held since the file was first added; the leak risk is operator discipline, not tooling. Watch for it in pre-commit / code review.
