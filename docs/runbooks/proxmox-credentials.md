# Proxmox credentials for Terraform

Terraform's bpg/proxmox provider authenticates as `root@pam` with username + password. PVE restricts a few config fields (VM `affinity`, arbitrary-path passthrough disks) to root, and the scoped API-token approach we tried earlier could not write either — even tokens derived from `root@pam` are rejected because PVE distinguishes "user logged in" from "token of that user." See `/work/AnsibleSpecs/decisions.md` "Proxmox VM CPU affinity" and "Disk passthrough on managed VMs".

`root@pam` has no MFA on this cluster, so direct password auth works without ceremony.

## Where the password lives

- **`terraform/{prd,scratch}/terraform.tfvars`** — gitignored (`*.tfvars` in `.gitignore`). The provider reads `proxmox_username` (defaulted to `root@pam`) and `proxmox_password` from there.
- **Roboform** — primary backup. Same entry as the PVE node root accounts.
- **Cloud folder with the SSH private keys** — secondary backup, in case the workstation is lost.

If the workstation is lost or the tfvars file is deleted, the password is recoverable from either backup.

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

The PVE root password is rotated by the operator on the cluster itself — not from this repo. After rotation:

1. Update Roboform.
2. Update `terraform.tfvars` in both `prd` and `scratch` checkouts.
3. Update the CI Jenkins credential entry once Phase 10 lands.

## Leak response

If `terraform.tfvars` leaks (for example, accidentally committed despite the `.gitignore` rule), rotate the PVE root password immediately on every cluster node — it's the same password across the cluster — and update the three locations above.

The `*.tfvars` `.gitignore` rule has held since the file was first added; the leak risk is operator discipline, not tooling. Watch for it in pre-commit / code review.
