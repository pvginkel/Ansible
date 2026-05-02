# 06 — Encrypted backup collector

## Goal

Build a small HTTP collector that ingests pre-encrypted backup blobs
from any source (k8s CronJobs, VM systemd timers, ad-hoc `curl`) and
streams them straight to cloud storage. Becomes the canonical
mechanism for getting backups off-box across the homelab — including
OpenBao's weekly JSON dump, which plan 05 currently routes via
direct rclone from `srvvault`.

The bulk of this work lives in `/work/HelmCharts` (the chart) and the
container build pipeline (the image). This plan tracks the design and
the consequences for this repo. Implementation pointers in the Steps
section name the right repo for each piece.

## Decisions taken with the operator

- **Push model with source-pinned age pubkey.** Each source holds the
  operator's age public key in its own config (committed in the
  source's repo, not supplied by the requester). Source generates the
  dump, encrypts to that pubkey, POSTs the ciphertext. The pubkey is
  authoritative because it's pinned at the source, not because it's
  trusted in transit.
- **No CephFS staging — the collector streams directly to cloud.**
  Earlier sketch had the collector buffering on a CephFS PVC and a
  separate sync job copying to cloud; simpler to skip the buffer and
  upload inline. Operator already depends on Ceph + k8s being up for
  many homelab services to function; no new coupling worth modelling.
- **No TLS in v1.** End-to-end age encryption is what protects the
  payload. TLS would only protect metadata (which source pushed
  what, when), and a passive observer on the homelab LAN is not in
  the v1 threat model. Add TLS in a later iteration once ingress /
  cert-manager is the path of least resistance.
- **Per-source bearer token authn.** A static map of `(token →
  source_name)` in a Helm secret for v1; migrates to OpenBao AppRole
  + ESO once Phase 6 is up. The API validates that the URL's source
  name matches the token's source name — a leaked token can spam its
  own source's path but not impersonate other sources.
- **Single cloud destination.** Whatever rclone-known backend the
  homelab already uses for daily-synced storage. Retention handled by
  cloud-side lifecycle rules or a small rclone-prune CronJob — not
  the collector's concern. The collector writes; retention reaps.
- **OpenBao's JSON dump is the first consumer.** Plan 05's "rclone
  copy to existing cloud-storage path" gets replaced with "POST to
  collector" before either lands. Saves writing the rclone-direct
  path only to migrate it later.
- **Implementation locations**:
  - `/work/HelmCharts` — new chart `backup-collector` (Deployment,
    Service, Helm secret carrying bearer tokens and rclone config,
    LoadBalancer or in-cluster Service).
  - Container image — built in the operator's existing image
    pipeline. Image carries the API server binary + the `rclone`
    binary; collector shells out to rclone for uploads.
  - `/work/Ansible` — only changes are (a) amend plan 05's OpenBao
    dump path to POST instead of rclone-direct, (b) a per-VM
    systemd-timer wrapper script in the future `openbao` role that
    produces the dump and POSTs it. No standalone Ansible-side
    collector lives here; the collector is k8s-hosted.

## API shape (v1)

```
PUT /v1/backup/<source>/<job>/<filename>
Authorization: Bearer <token>
Content-Type: application/octet-stream

<encrypted blob>
```

- `<source>` — short name, e.g. `openbao`, `keycloak`, `pve-configs`.
- `<job>` — sub-name within a source, e.g. `kv-dump`, `realm-export`.
- `<filename>` — source picks; convention is
  `<UTC-ISO8601>.age` so cloud storage sorts naturally.
- Server validates: token is known, token's source matches the URL
  `<source>`. On success, streams the body to
  `cloud:backups/<source>/<job>/<filename>`. Returns `201 Created`
  with the cloud path in the body for log-trail purposes.
- On token mismatch: `401`. On rclone failure: `502`. On already-
  exists: `409` (don't silently overwrite — sources should pick
  unique filenames; collisions imply a bug).

## Steps

### `/work/HelmCharts` — new chart `backup-collector`

- `Deployment`: single replica (it's a SPOF for ingestion, not for
  served data; restart-on-crash is enough for v1). Liveness probe on
  `/healthz`. Resource requests sized for "stream a few MB through
  rclone" — a few hundred MB RAM, a half core.
- `Service`: ClusterIP for in-cluster sources; MetalLB IP for VM
  sources. Document the IP in `static-hosts.yaml` so `backup.home`
  resolves cleanly.
- `Secret` (helm-managed for bootstrap): one map of `(token →
  source_name)`, one rclone config blob. Migrated to ESO-from-OpenBao
  once Phase 6 is up — the chart structure shouldn't change, only the
  source of the secret.
- Chart values control the cloud destination prefix
  (`cloud:backups`), the source list, and per-source token names.
  Tokens themselves live in the secret, never in values.

### Container image

- Operator-owned image build pipeline (same shape as
  `modern-app-dev` from plan 04 — Jenkins job → archive → bake into
  image).
- Base on a minimal distro (Alpine or distroless), embed the rclone
  binary alongside the API server.
- API server language is the operator's call. ~50–100 lines in
  any of FastAPI, Express, Go, etc. Whatever's quickest.

### `/work/Ansible` — amend plan 05

In `docs/plans/05-openbao-static-seal.md` (this repo):

- "OpenBao backup / DR" section's recovery path mentions cloud
  storage as the destination. Update the dump-upload mechanism from
  "rclone copy to existing daily-synced cloud-storage path" to "PUT
  to the backup collector at `https://backup.home/v1/backup/openbao/
  kv-dump/<UTC-ISO8601>.age` with a per-source bearer token."
- The decryption side is unchanged — the operator pulls the file
  from cloud storage (via rclone or the cloud UI) and decrypts with
  the Roboform-held age private key. The collector is on the write
  path only.
- The rest of plan 05's design (3-node Raft, leader-only timer,
  vzdump exclusion, etc.) is unchanged. Only the upload step
  changes.

When plan 05 lands its `decisions.md` write, the OpenBao backup
section should reflect the collector path. If plan 05 lands first,
that section gets a follow-up edit when plan 06 lands.

### `/work/Ansible` — `openbao` role (Phase 6)

- The leader-guarded weekly timer's wrapper script becomes:
  ```
  if leader: bao operator raft snapshot save - | jq <transform> | \
      age -r <pubkey> | \
      curl -X PUT -H "Authorization: Bearer $TOKEN" \
           --data-binary @- \
           "https://backup.home/v1/backup/openbao/kv-dump/$(date -u +%FT%TZ).age"
  ```
  (Sketch; Phase 6 finalizes the dump format — `bao operator raft
  snapshot save` vs. a per-mount KV walk vs. the existing JSON-dump
  approach are decided during role design.)
- Bearer token comes from a file written by the role (mode 0400,
  owner openbao). Source of the token is ansible-vault for v1; ESO
  + OpenBao once both are running and the bootstrap chicken-and-egg
  is resolved.

### `/work/HelmCharts` — per-consumer CronJobs

For each in-cluster source that wants to back up (Keycloak realm
exports, future databases, etc.):

- Add a `CronJob` to the source's chart that runs the dump, pipes
  through age, POSTs to the collector. Pattern is identical across
  charts; suggest a shared template helper or a tiny library chart
  if more than two consumers land.
- Tokens injected via env vars from the chart's Helm secret (or
  ExternalSecret post-Phase-6).

Not in scope for plan 06: enumerating which charts get this. That's
per-consumer work, scheduled as needs arise.

## Verification

- **Image build**: Jenkins produces the collector image, tagged.
- **Chart deploy**: chart deploys to the dev cluster, pod is
  Running, `/healthz` returns 200.
- **Smoke test (auth)**: `curl -X PUT` with a missing/invalid token
  returns `401`. With a valid token but wrong source name in the
  URL: `401`.
- **Smoke test (happy path)**: `curl -X PUT` with a valid token, a
  small age-encrypted file as body, succeeds with `201` and the
  cloud-path response. Verify the file appears in cloud storage at
  the expected path; verify it decrypts with the operator's age
  private key.
- **OpenBao integration drill** (part of Phase 6's recovery drill):
  the leader's timer fires, the dump lands in cloud storage,
  decrypts cleanly, replays into a scratch OpenBao to validate the
  round-trip.

## Caveats

- **HTTP only in v1.** A passive observer on the homelab LAN sees
  which source pushed which job at what time. The body is age
  ciphertext; no plaintext leaks. Add TLS once it's the path of
  least resistance (cert-manager + ingress is probably the
  trigger).
- **Replay attacks**: an attacker who captured one PUT in flight
  could replay it to spam the destination with duplicates. v1
  mitigation: collector returns `409` on filename collision, so a
  literal replay is rejected if the source picks unique filenames
  (timestamps satisfy this). No content-hash dedupe in v1.
- **Collector is a SPOF for ingestion.** If it's down, sources fail
  their POST and skip a backup cycle. Mitigations: restart-on-crash,
  `OnFailure` on source CronJobs, an alert when expected sources
  haven't pushed in 25 hours. HA (multiple replicas + LB) is a
  later improvement; v1 single-replica is enough.
- **Token leak blast radius**: a leaked token can spam its own
  source's path or fill cloud storage with garbage. It can't read
  existing backups (they're age-encrypted; the token doesn't grant
  decrypt rights), can't impersonate other sources (URL/source
  binding), and can't escape its own prefix. Acceptable surface.
- **The collector itself doesn't validate that the body is actually
  age-encrypted.** A buggy or malicious source could push plaintext;
  the collector would happily upload it. Optional v2: refuse bodies
  whose first bytes don't match `age-encryption.org/v1`. Cheap; not
  in v1.
- **Cross-repo coordination.** Most of the work is outside this
  repo. The Ansible side stays small (plan 05 amendment + the
  Phase 6 role's wrapper script). If the chart and image lag the
  Phase 6 role's expectations, the role's wrapper falls back to
  the rclone-direct path defined in plan 05 — keeps Phase 6
  unblocked.

## Commits

1. This plan, here in `docs/plans/06-backup-collector.md`. Single
   commit; the design is the trackable artefact for this repo.
2. Plan 05 amendment (when both plans are about to land):
   `docs/plans/05-openbao-static-seal.md` updates the OpenBao dump
   destination from rclone-direct to the collector. Separate commit
   if the timing diverges; folded into plan 05's commit if they
   land together.
3. Implementation commits live in `/work/HelmCharts` (chart) and
   the container build pipeline (image). Out of scope for this
   repo.
