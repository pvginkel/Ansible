# DNS reservation sidecar API

Specification for the API exposed by the dnsmasq sidecar that adds, updates, and removes DNS+DHCP reservations alongside the existing operator-curated static file. Covers the contract only; persistence layout, dnsmasq config rendering, and reload mechanics are implementation choices left to the sidecar.

Pairs with [`dns-reservation-terraform.md`](dns-reservation-terraform.md), which specifies the Terraform-side consumer.

## Scope

- Manages **dynamic** reservations: entries created and destroyed in lockstep with infrastructure provisioning. The first (and currently only) client is Terraform.
- Coexists with **static** reservations defined in `static-hosts.yaml` (HelmCharts) — operator-curated entries for printers, IoT, network gear, and any unmanaged hosts.
- Each reservation is a triple: hostname, MAC, IPv4. IPv6 and DNS-only entries are not supported here; they belong in the static file.

## Resource model

A reservation is identified by **hostname**, which is the URL key. MAC and IPv4 are reservation attributes.

Why hostname as the ID:
- Already unique by DNS contract.
- Stable across MAC rotations and IP renumbering.
- The natural label for humans and for Terraform resource names.

## Namespaces

Two namespaces share the dnsmasq config:

| Namespace | Source | Mutated by |
|---|---|---|
| **static** | `static-hosts.yaml` | Operator, by hand |
| **dynamic** | This API | Terraform (and any future API client) |

A given hostname, MAC, or IPv4 lives in **exactly one** namespace at a time. The sidecar enforces this by rejecting writes that would shadow or collide with a static entry. See *Conflict semantics* below.

## Endpoints

All paths under `/reservations`. JSON request and response bodies; UTF-8.

### `PUT /reservations/{hostname}`

Idempotent create-or-update. Body:

```json
{ "mac": "02:a7:f3:03:84:00", "ipv4": "10.1.0.42" }
```

Responses:

- `201 Created` — reservation did not exist; created.
- `200 OK` — reservation existed and was updated, or already in the requested state.
- `400 Bad Request` — malformed body, invalid MAC, invalid IPv4.
- `401 Unauthorized` — missing or invalid token.
- `409 Conflict` — see error codes.

Body of `200`/`201`:

```json
{ "hostname": "srvk8sl1", "mac": "02:a7:f3:03:84:00", "ipv4": "10.1.0.27" }
```

### `GET /reservations/{hostname}`

Read a single dynamic reservation. Static entries are not visible through this endpoint — they live in a different namespace.

- `200 OK` with reservation body.
- `404 Not Found` — no dynamic reservation under this hostname (even if a static entry exists).
- `401 Unauthorized`.

### `DELETE /reservations/{hostname}`

Remove a dynamic reservation. Static entries cannot be deleted through this API.

- `204 No Content` — removed.
- `404 Not Found` — no dynamic reservation under this hostname.
- `401 Unauthorized`.

### `GET /reservations`

List all dynamic reservations. Provided for human inspection only; **Terraform does not call this** (see *Out of scope*).

- `200 OK`:
  ```json
  { "reservations": [ { "hostname": "...", "mac": "...", "ipv4": "..." }, ... ] }
  ```
  No pagination — fleet is small.
- `401 Unauthorized`.

### `GET /healthz`

Unauthenticated liveness probe. `200 OK` with `{"status":"ok"}` if the sidecar can read its persistent store and serve traffic.

## Conflict semantics

Conflicts are checked across **both** namespaces.

- A `PUT` re-asserting an existing dynamic entry's current MAC and IPv4 is fine (idempotent, returns `200`).
- A `PUT` whose hostname is claimed by a **static** entry → `409 hostname_static`.
- A `PUT` whose MAC is held by a **different** hostname (in either namespace) → `409 mac_conflict`.
- A `PUT` whose IPv4 is held by a **different** hostname (in either namespace) → `409 ipv4_conflict`.

The check is "different hostname" — updating a dynamic entry's own MAC or IPv4 in place is allowed.

## Errors

Error responses share a JSON envelope:

```json
{ "error": "mac_conflict", "message": "MAC 02:a7:f3:03:84:00 is already reserved for srvk8ss1" }
```

| Code | Status | Meaning |
|---|---|---|
| `bad_request` | 400 | Malformed body or missing field. |
| `invalid_mac` | 400 | MAC fails format check. |
| `invalid_ipv4` | 400 | IPv4 fails parse. |
| `unauthorized` | 401 | Missing or invalid bearer token. |
| `not_found` | 404 | (`GET`/`DELETE`) no dynamic reservation under this hostname. |
| `hostname_static` | 409 | Hostname is claimed by the static file. |
| `mac_conflict` | 409 | MAC reserved by another hostname. |
| `ipv4_conflict` | 409 | IPv4 reserved by another hostname. |
| `internal` | 500 | Anything else; persistence-store failures, unexpected errors. |

## Validation

- **MAC**: matches `^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$`. Stored and returned lowercase; the API normalizes on input.
- **IPv4**: standard dotted-quad, must parse to a valid address. No subnet check (the sidecar does not own the IP plan).
- **Hostname**: matches `^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`. Lowercase only. No FQDN — the domain is appended by dnsmasq.

## Auth

Bearer token: `Authorization: Bearer <token>`. Single shared secret to start; one token, all endpoints. The token is delivered to the sidecar via Helm/Secret. Rotation is a Helm-side operation and out of scope for this spec.

`/healthz` is the only unauthenticated endpoint.

## Persistence

The sidecar persists reservations across restarts. Format and storage are implementation choices, with three contractual guarantees:

- A restart must not drop or corrupt entries; an uncommitted in-flight write is the only allowable loss.
- A clean shutdown must flush before exit.
- Concurrent writes are serialized. Single-writer is acceptable; this is not a hot path.

## Reload

After `PUT` or `DELETE` returns 2xx, the change is durable. The operator may rely on dnsmasq picking the change up within **5 seconds**. How that propagation happens (SIGHUP, file watch, regenerate-and-restart) is the sidecar's concern.

## Out of scope

- **No bulk / list-then-reconcile endpoint.** Each Terraform resource manages its own entry by id. A "set the full set of dynamic reservations to this list" endpoint would invite a future client that nukes operator-curated additions made through other tools, and is explicitly rejected by this design.
- **No IPv6.** Managed VMs are IPv4-only on `vmbr0`. IPv6 entries stay in the static file if needed.
- **No DNS-only reservations.** A reservation is always (hostname, MAC, IPv4). Hostnames that need only an A record (no DHCP) belong in the static file.
- **No CNAME, SRV, TXT, or other record types.**
- **No reservation history or audit log.** If needed later, it lives behind a different endpoint.
- **No API versioning.** Single internal client. If a breaking change is ever needed, version the path then.
