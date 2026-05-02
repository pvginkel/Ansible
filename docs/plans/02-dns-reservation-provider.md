# 02 — Wire the homelab DNS reservation provider into per-VM modules

## Goal

Land the `pvginkel/homelab` provider's `homelab_dns_reservation` resource in
the per-VM Terraform module so each VM and its dnsmasq reservation are
created together. Drops the manual reservation step from the rebuild
runbook before Phase 4c hits it.

This is the `homelab_dns_reservation` half of Phase 9 landing early. The
sidecar API and the resource shape are specified in
[`docs/specs/dns-reservation-api.md`](../specs/dns-reservation-api.md) and
[`docs/specs/dns-reservation-terraform.md`](../specs/dns-reservation-terraform.md).
The provider source lives at `/work/HomelabTerraformProvider`.

Decisions taken with the operator:

- The sidecar is deployed in production and reachable.
- For multi-NIC VMs (k8s, ceph), only the **vmbr0** NIC gets a
  `homelab_dns_reservation`. The vmbr0-tag-2 (k8s workload VLAN,
  10.2.0.0/16) and vmbr1 (backplane) NICs carry static addresses declared
  directly in the per-VM TF entry. No second reservation, no client-side
  IP picking on those subnets.
- Tag-2 addresses and vmbr1 addresses are statically managed. They do not
  mirror the vmbr0 address. Instead they keep the current addresses. The
  tag-2 interface is wholly owned by the Kubernetes cluster. The vmbr1
  interface, the backlane, is shared with ceph and some other nods. Reuse
  the current addresses on rebuild.

## Steps

### Provider source pin

`terraform/prd/versions.tf` and `terraform/scratch/versions.tf`:

- Add `homelab = { source = "pvginkel/homelab" }` to `required_providers`.
- No version constraint — the provider runs from a per-machine dev override
  via `~/.terraformrc`. Operator-workstation runbook already documents
  `~/.terraformrc`; add a line for the homelab dev override if it isn't
  there yet.

### Provider block

`terraform/prd/providers.tf` and `terraform/scratch/providers.tf`:

```hcl
provider "homelab" {
  dns_reservation_url   = var.dns_reservation_url
  dns_reservation_token = var.dns_reservation_token
}
```

### Variables

`terraform/prd/variables.tf` and `terraform/scratch/variables.tf`:

- Add `variable "dns_reservation_url"` (string).
- Add `variable "dns_reservation_token"` (string, sensitive).
- Add both to `terraform.tfvars.example` placeholders.

### Module

`terraform/modules/managed-vm/main.tf`:

- Add a precondition asserting `var.network_devices[0].bridge == "vmbr0"`
  and `var.network_devices[0].vlan_id == 0`. The convention is that
  `network_devices[0]` is the vmbr0 reservation NIC; surface a violation at
  plan time, not at boot.
- Add the resource:

  ```hcl
  resource "homelab_dns_reservation" "this" {
    hostname = var.name
    mac      = var.network_devices[0].mac_address
  }
  ```

- Add `depends_on = [homelab_dns_reservation.this]` to
  `proxmox_virtual_environment_vm.this` so the reservation lands before
  DHCP fires on first boot.

`terraform/modules/managed-vm/outputs.tf`:

- Expose `dns_ipv4 = homelab_dns_reservation.this.ipv4` for downstream
  consumers (Ansible inventory generators, etc.).

### Static addressing for tag-2 and vmbr1

These NICs are owned at provision time, not by dnsmasq.

- The per-VM module already takes a `network_devices` list with VLAN id —
  no module change needed for the *Terraform-side* declaration.
- Guest-side configuration (netplan / cloud-init network-config) is **out
  of scope for this plan**. It is handled per VM in the Phase 4c rebuild
  work, when each k8s VM's cloud-init template is wired to render the
  static addresses for its second and third NICs.
- This plan only locks in the convention: tag-2 / vmbr1 addresses are
  static, declared in the per-VM `vms.tf` entry, no IPAM, no reservation
  resource.

### Import existing reservations

For each currently-managed VM, import the existing dnsmasq entry into TF
state. Operator runs the `terraform import` command; Claude prepares the
exact list.

VMs to import:

- prd: `srvk8sl1`, `srvk8ss1`, `srvk8ss2`, `srvceph1`, `srvceph2`, `srvceph3`.
- scratch: `wrkscratchk8s1`, `wrkscratchk8s2` (only if the scratch fleet
  carries reservations — verify with `GET /reservations` against the
  sidecar before adding).

After imports, `terraform plan` is no-op on every reservation block.

### Decisions doc

`docs/decisions.md`:

- "MAC addressing for managed VMs": drop the "Until Phase 9 lands,
  reservations are added by hand before `terraform apply`" caveat.
  Reservation is now part of the apply.
- "DNS and hostnames": reflect the same change — managed-VM reservations
  flow through TF, not the operator's hand.
- "Network topology for managed VMs": add the static-address convention
  for tag-2 (sequential within `10.2.0.0/16`) and vmbr1 (per-VM static,
  declared in `vms.tf`).

### Runbook

`docs/runbooks/k8s-rebuild.md`:

- Drop the "operator manually adds dnsmasq reservation" step.
- Replace with a note that the reservation is part of the per-VM TF module
  — `terraform apply` creates both, `terraform destroy` removes both in
  reverse order.

`docs/runbooks/operator-workstation.md`:

- Add a `~/.terraformrc` line for the homelab provider dev override if not
  already there.

## Verification

- After provider/module edits and before imports: `terraform plan` proposes
  to **create** every reservation block. This is expected — state has no
  reservations yet.
- After imports: `terraform plan` is no-op on every reservation block.
- For each (hostname, MAC, IPv4) imported, verify against the sidecar by
  hand (`GET /reservations/{hostname}`). If any triple disagrees with what
  bpg's import recorded, fix the sidecar entry before applying anything
  else.
- Exercise the create path on the scratch fleet end-to-end: destroy one
  scratch VM, re-apply, confirm `depends_on` orders the reservation
  before the VM and that the VM picks up its DHCP-allocated address from
  dnsmasq on first boot.

## Caveats

- The provider runs from a local dev override; no version pin, no
  module-registry release. CI (Phase 10) will need its own override or a
  published provider — defer that question.
- Existing reservations in dnsmasq must match what the imports record.
  Verify each (hostname, MAC, IPv4) triple before importing — if dnsmasq
  has stale data, the import looks clean now but plan will diverge on the
  next refresh.
- The bearer token is sensitive; lives in tfvars (gitignored).

## Commits

1. TF change: `versions.tf`, `providers.tf`, `variables.tf`, module, tfvars
   example.
2. Imports: a single commit after all imports succeed (state file
   change + no source diff). Operator's keystroke for the import command;
   Claude verifies and commits.
3. Docs: `decisions.md` + runbook updates.
