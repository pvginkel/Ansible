# Proxmox API token for Terraform

Terraform authenticates to Proxmox via an API token owned by a dedicated service user. This runbook creates that user and token, assigns the minimum privilege set, and records where the secret lives locally.

Do this once per fresh Proxmox cluster. Output: a `PROXMOX_VE_API_TOKEN` value in the form `terraform@pve!automation=<uuid-secret>` that `bpg/proxmox` can consume.

## 1. Create the service user

Proxmox web UI → **Datacenter → Permissions → Users → Add**

| Field | Value |
|---|---|
| User name | `terraform` |
| Realm | `Proxmox VE authentication server` (`pve`) |
| Enabled | ✅ |
| Password | any — it's never used; token auth only |

Do *not* use `root@pam`. We want a dedicated, revocable identity.

## 2. Create a custom role with the privileges Terraform needs

Proxmox web UI → **Datacenter → Permissions → Roles → Create**

| Field | Value |
|---|---|
| Name | `TerraformProv` |
| Privileges | see list below |

Privileges to tick (covers VM lifecycle + cloud-init + image download + pool management — the full set needed by `bpg/proxmox`):

```
Datastore.Allocate
Datastore.AllocateSpace
Datastore.AllocateTemplate
Datastore.Audit
Pool.Allocate
Sys.Audit
Sys.Console
Sys.Modify
VM.Allocate
VM.Audit
VM.Clone
VM.Config.CDROM
VM.Config.Cloudinit
VM.Config.CPU
VM.Config.Disk
VM.Config.HWType
VM.Config.Memory
VM.Config.Network
VM.Config.Options
VM.Migrate
VM.Monitor
VM.PowerMgmt
SDN.Use
```

## 3. Grant the role to the user at the root path

Proxmox web UI → **Datacenter → Permissions → Add → User Permission**

| Field | Value |
|---|---|
| Path | `/` |
| User | `terraform@pve` |
| Role | `TerraformProv` |
| Propagate | ✅ |

Granting at `/` keeps the config simple; scoping tighter (e.g. only on `/vms`, `/storage/local-lvm`, `/storage/local`) is possible but brittle in a homelab.

## 4. Create the API token

Proxmox web UI → **Datacenter → Permissions → API Tokens → Add**

| Field | Value |
|---|---|
| User | `terraform@pve` |
| Token ID | `automation` |
| **Privilege Separation** | **❌ unchecked** — the token inherits the user's privileges directly |
| Expire | blank (no expiry) |

Proxmox shows the token secret **once**. Copy it immediately.

The full token identifier is:

```
terraform@pve!automation=<uuid-secret>
```

## 5. Store the secret locally

Terraform reads credentials from `terraform/terraform.tfvars` (gitignored — `*.tfvars` is in `.gitignore`). Copy the example file and fill it in:

```sh
cd terraform
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars`:

```hcl
proxmox_endpoint  = "https://pve.home:8006/"
proxmox_api_token = "terraform@pve!automation=<uuid-secret>"
proxmox_insecure  = true   # PVE default cert is self-signed; flip to false once a real cert is in place
```

Also save the token to Roboform and the same cloud folder that holds the SSH private keys. If the local checkout is lost, the token is recoverable; if it's leaked, revoke and regenerate.

## 6. Verify

From the repo root:

```sh
cd terraform
terraform init
terraform plan
```

A successful `terraform plan` (even one that produces no resources yet) confirms the token + endpoint work.

## Revocation / rotation

- Revoke the token: **Datacenter → Permissions → API Tokens**, select `terraform@pve!automation`, **Remove**.
- Issue a new one with the same name and update `terraform.tfvars`.
- Disable the user entirely (emergency): untick **Enabled** on the `terraform@pve` user.
