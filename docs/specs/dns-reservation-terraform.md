# Terraform DNS reservation resource

Specification for the Terraform resource that creates, updates, and destroys reservations through the [DNS reservation sidecar API](dns-reservation-api.md).

## Scope

- A single resource type managing one (hostname, MAC, IPv4) reservation per instance.
- Used inside per-VM Terraform modules so a VM and its reservation are created, updated, and destroyed together in one apply.
- Does **not** pick or validate IPs. The API allocates the IPv4 from its configured CIDR; the resource exposes the allocated address as a computed attribute.

## Provider choice

Two viable shapes:

1. **Custom provider** in Go using the Terraform plugin framework. Type-safe schema, real diffs, clean UX across many per-VM modules. Most of the work is scaffolding; the API surface is small.
2. **Community `restapi` provider** (`Mastercard/restapi`). Faster to land; no Go build pipeline; correspondingly clunkier resource definitions in callers.

**Recommendation: (1) custom provider** when this lands, especially since the same provider is the natural home for any future sidecar resource types (CNAMEs, etc.). `restapi` is acceptable as a stopgap — the resource shape below is identical either way, and migrating between them is a state-mv exercise, not a redesign.

## Resource: `dnsreservation_reservation`

### Inputs

| Argument | Type | Required | ForceNew | Notes |
|---|---|---|---|---|
| `hostname` | string | yes | yes | The reservation key. Renaming is destroy+create. |
| `mac` | string | yes | no | Lowercase, colon-separated. Validated client-side against `^[0-9a-f]{2}(:[0-9a-f]{2}){5}$`. |

### Computed

| Attribute | Type | Notes |
|---|---|---|
| `id` | string | Equals `hostname`. |
| `ipv4` | string | Allocated by the API on first `PUT` and bound to the hostname for its lifetime. Read back from `GET` on refresh. |

### Lifecycle

| Op | API call |
|---|---|
| Create | `PUT /reservations/{hostname}` with `{"mac": ...}`. Response carries the allocated `ipv4`, which is stored in state. |
| Read (refresh) | `GET /reservations/{hostname}`. `404` → resource is gone, mark for recreate. |
| Update | `PUT /reservations/{hostname}` with the new `{"mac": ...}`. The API preserves the existing `ipv4` across MAC changes. |
| Delete | `DELETE /reservations/{hostname}`. `404` is treated as success (already gone). |

### Drift behavior

- Out-of-band edits to MAC surface as a normal plan diff on next refresh.
- The API binds `ipv4` to the hostname for its lifetime, so the computed value should not change in normal operation. If the sidecar's persistent store is lost and the hostname is later re-allocated to a different address, refresh updates the computed `ipv4` in state — no plan diff, since `ipv4` is not an input.
- Out-of-band deletion (someone removed the entry directly through the API) shows as a recreate on next plan.

### Import

`terraform import dnsreservation_reservation.foo srvk8sl1` issues `GET /reservations/srvk8sl1` and populates state. `404` → import error.

## Provider configuration

```hcl
provider "dnsreservation" {
  url   = "http://dns-reservations.home"
  token = var.dns_reservation_token   # sensitive
}
```

| Argument | Required | Notes |
|---|---|---|
| `url` | yes | Base URL of the sidecar. Path `/reservations` is appended by the provider. |
| `token` | yes | Bearer token. Marked `Sensitive`. |

No retry/backoff configuration; the standard HTTP client is sufficient. The sidecar is on-LAN and failure modes are operator-visible (token expired, sidecar down) and best surfaced as a plain apply error.

## Wiring into a per-VM module

Each per-VM module gets one reservation resource alongside its `proxmox_virtual_environment_vm`. Sketch:

```hcl
resource "dnsreservation_reservation" "this" {
  hostname = var.name
  mac      = local.mac     # already computed from var.vm_id
}

resource "proxmox_virtual_environment_vm" "this" {
  name = var.name
  # ...
  network_device {
    bridge      = "vmbr0"
    mac_address = local.mac
  }

  depends_on = [dnsreservation_reservation.this]
}
```

`depends_on` ensures the reservation exists before the VM fires its first DHCP request. On destroy, Terraform reverses the order automatically: VM destroyed first, reservation removed second.

The VM does cloud-init DHCP on `vmbr0` (see `modules/managed-vm/main.tf`), so the allocated `ipv4` does not need to flow back into the VM resource — dnsmasq hands it to the guest at boot. The computed `dnsreservation_reservation.this.ipv4` is available for outputs or downstream consumers (Ansible inventory generation, etc.) that need to know the address.

## Module inputs added per VM

None. `hostname` reuses the module's existing `name` input; `mac` is already computed from `vm_id` per the MAC-addressing scheme in [`docs/decisions.md`](../decisions.md). The IPv4 is server-allocated and exposed as a computed attribute.

## Out of scope

- **No client-side IP picking.** The API owns the allocation policy; the resource has no `ipv4` input. See the [API spec](dns-reservation-api.md) for allocation order and CIDR ownership.
- **No CNAME, SRV, TXT, or other record types.** Only the (hostname, MAC, IPv4) triple this resource owns.
- **No multi-NIC reservations.** Managed VMs with multiple NICs (k8s, Ceph) get a reservation only for their `vmbr0` NIC; backplane (`vmbr1`) and workload-VLAN (`vmbr0` tag 2) NICs carry static IPs configured at provision time, not via dnsmasq.
- **No IPv6.** See the [API spec](dns-reservation-api.md).
- **No reservation transfer / rename.** Hostname is `ForceNew`; renaming a VM is a destroy+create.
