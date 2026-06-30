# BuildKit + forward-compatible service mTLS / authorization

**Status:** Working document (handover). Pre-slice.
**Driver:** Triage #113 "Setup buildkit" (https://trello.com/c/OtuhDYQS/113-setup-buildkit).
**Date:** 2026-06-30.
**Relationship to the arch-design doc:** `../AnsibleSpecs/change_requests/buildkit_daemon/design_buildkit.md`
remains the detailed grounding (file:line citations for the storage / ESO / step-ca /
registry / KubeCoder patterns). Read it alongside this one — but note its **privileged-mode
assumption is superseded** by Decision 1 below.

## Why this document exists

Card #113 began as "stand up a BuildKit daemon," but the real driver is **KubeCoder**: build
images from inside a dev-env pod and run them as a Kubernetes Job. Designing it surfaced a
question bigger than BuildKit — **how to do mutual TLS + authorization between services** in a
way that's proportionate for a homelab yet forward-compatible as more services need it. This
doc records the decisions taken and lays out the security model, so the BuildKit slice *and*
future service slices can be authored from it. It's written to hand over to a fresh context.

## Decisions locked (this conversation)

1. **BuildKit runs rootless. Non-negotiable.** A privileged container is effectively root on
   srvk8s4 behind a trivially-defeated namespace veneer, and build inputs are *not* trusted
   (KubeCoder clones external repos and runs Claude Code, i.e. arbitrary code). If rootless
   proves infeasible on srvk8s4, **we stay on Kaniko** rather than run privileged. This
   supersedes Requirement #1 of the arch-design doc.
2. **Because it's rootless, BuildKit accepts requests from any homelab participant.** The
   node-root exposure is gone, so there's no need to restrict *who* may build. mTLS is still
   used — for channel encryption and "must be a homelab participant" — **not** for
   least-privilege between clients.
3. **Endpoint: LoadBalancer + DNS, exposed on the home LAN** (mirroring step-ca's `ca.home`).
   Address `buildkit.home` (pinned MetalLB IP + webathome DNS annotation); server-cert SAN =
   `buildkit.home`. Reachable by in-cluster pods (Jenkins, KubeCoder) and external hosts
   (workstation, VMs). This drops the earlier CoreDNS bare-name pin idea.
4. **No NetworkPolicy / no defense-in-depth gating.** mTLS is the only access control — deliberately.
5. **No per-service CA hack for BuildKit.** We had considered a dedicated buildkit CA so
   buildkitd (which only checks cert-chain-to-CA) could be made selective. Rootless removes the
   need: authorization, where required, lives in a *policy layer* (below), not the trust anchor.
   **One homelab CA** for identity.
6. **Two slices.** (a) **Daemon slice** — HelmCharts chart + Ansible node-prep + cache PV +
   LB/DNS + rootless config; independently testable via a `buildctl` smoke build. (b)
   **Client-enablement slice** — out-of-tree: the `buildkit()` helper in JenkinsPipelineUtils,
   the Jenkins pod template (lives unversioned in `JENKINS_HOME`), and the KubeCoder controller's
   endpoint/cert injection + Job-creation RBAC.
7. **Registry auth is a separate, bigger effort** (its own Triage card). `registry:5000` is
   unauthenticated HTTP *today* — anything in-cluster can already overwrite `:latest` of any
   image, which the version-poller then auto-deploys. BuildKit doesn't worsen this; it inherits
   the registry's posture. Securing it (TLS + auth, and updating every push/pull consumer incl.
   Kaniko and the version-poller) is its own slice.
8. **The general "cert-please" issuance mechanism is a separate platform track** (below), not a
   BuildKit prerequisite. BuildKit needs client certs only for its handful of consumers, which
   the existing patterns cover.

## The security model

### Principle: separate authentication from authorization

mTLS gives **encryption** and **authentication** (a verified identity). It does **not** give
**authorization** (what that identity may do). BuildKit is the cautionary example: it only
verifies that a client cert chains to its configured CA — authentication, with no notion of
"which clients may build." Keep the two concerns separate:

- **Authentication / identity** — every workload and host carries an mTLS client cert from
  step-ca, with a stable principal in the SAN.
- **Authorization** — a *per-service policy* maps principals → allowed actions, enforced at the
  service or a proxy in front of it.

This is exactly what a service mesh does (Istio = SPIFFE identities + Envoy enforcing
`AuthorizationPolicy`). We build the same model **without the mesh control plane** —
proportionate for a homelab, and the concepts transfer 1:1 if we ever adopt one.

### Identity layer (authentication)

- **One homelab CA** (the existing step-ca). No per-service roots — that doesn't scale, and
  authz belongs in policy, not the trust anchor.
- Every participant gets a client cert. Recommended principal: a **URI SAN in SPIFFE form** —
  `spiffe://homelab/ns/<namespace>/sa/<serviceaccount>` for in-cluster workloads,
  `spiffe://homelab/host/<hostname>` for VMs/hosts. (VMs already get host certs via
  `internal_tls`; this standardizes the *identity string* that policy keys on — it's what
  mesh/SPIRE use.)
- **Issuance** — two patterns exist today: `internal_tls` (JWK split-issuance, VMs) and ACME via
  certbot (in-cluster HTTP servers). For *workload client certs into a Secret* there's a gap →
  the "cert-please" track below.

### Authorization layer — how access rules are defined

This is the core question. **Authorization is per-service policy data, keyed on the client
principal, enforced at a thin proxy.** Each service is one of two modes:

- **`open`** — accept any valid homelab cert; authentication only. (BuildKit-rootless and the
  backup server are `open`: "anyone may build", "anyone may back up".)
- **`restricted`** — only listed principals may connect, optionally scoped to operations.

A restricted service's policy is **declarative data, version-controlled with the service** (in
HelmCharts values), e.g.:

```yaml
authz:
  mode: restricted
  allow:
    - principal: spiffe://homelab/ns/jenkins-prd/*     # any workload in jenkins-prd
    - principal: spiffe://homelab/host/wrkdev
      methods: [GET]                                   # optional L7 scoping (HTTP)
```

**Enforcement** — a thin proxy in front of the service terminates client mTLS and checks the
principal against the policy. Two proportionate implementations (standardize on one):

- **Envoy sidecar + RBAC filter** — production-grade, principal-based rules; literally the config
  Istio generates, minus the control plane. Static config, no operator.
- **nginx** (`ssl_verify_client on` + `map $ssl_client_s_dn` / SAN) — lighter, already deployed
  here, fine for HTTP and gRPC (`grpc_pass`).

**Escalation path** (don't build yet, but know it exists): for policy beyond allow-lists, the
production answer is **OPA (Open Policy Agent)** with Rego, called from Envoy via `ext_authz`.
Static Envoy RBAC / nginx maps cover everything foreseeable here.

### What each off-the-shelf tool actually is (so we don't reach for the wrong one)

- **Service mesh (Istio / Linkerd)** — the full answer: auto-mTLS + central authz + encryption
  via per-pod sidecars. Correct at scale, **overkill here** (control plane + sidecar-per-pod).
- **SPIFFE / SPIRE** — workload-identity issuance + attestation; the grown-up version of the
  "cert-please" mechanism. Identity only — the receiver still enforces policy.
- **cert-manager** — workload cert *issuance* (Certificate CRD → Secret). Rejected here for the
  server-cert/nginx path, but it's the standard fit for *this* job; reconsider deliberately vs a
  homegrown issuer.
- **Keycloak** — **wrong layer**: a *user* OIDC/OAuth IdP. It *can* do service-to-service authz
  via OAuth2 client-credential JWTs (scopes the receiver validates) — a legitimate L7
  alternative to mTLS — but it's token-based, not channel/identity mTLS, and BuildKit doesn't
  speak it.

### Where BuildKit sits in this model

BuildKit is the **first consumer**, and an `open` one:

- Rootless daemon, `--tlscacert` = homelab root, accepts any homelab client cert (authn +
  encryption only; no proxy, no authz — it's `open`).
- Consumers: Jenkins build pods, KubeCoder env pods, external hosts — each presents a homelab
  client cert. External hosts use their existing host cert; in-cluster consumers get a
  client-cert Secret (existing distribution patterns suffice for these few).
- It exercises the **identity layer** for real and proves the model end-to-end, without needing
  the authz layer yet. **The first `restricted` service is what will drive building the
  proxy + policy pattern** — that's the forward-compatible payoff.

## BuildKit slice specifics (from the arch-design doc, with the rootless delta)

- **Rootless deployment shape:** `moby/buildkit:*-rootless` via `rootlesskit`; non-root
  `securityContext` (`runAsUser`); cache at the rootless path (`~/.local/share/buildkit`) on the
  zpool5 PVC. Verify whether seccomp/AppArmor must be `unconfined` or
  `--oci-worker-no-process-sandbox` is required (kernel-dependent).
- **Node prerequisite (Ansible, srvk8s4) — the gating spike:** unprivileged user namespaces must
  be usable. Recent Ubuntu ships `kernel.apparmor_restrict_unprivileged_userns=1`, which can
  block rootless; a node-prep task (sysctl and/or an AppArmor profile, plus `fuse-overlayfs` if
  the kernel can't do overlayfs-in-userns) is likely needed. **Validate this on srvk8s4 before
  committing to the slice — it's the gate on "rootless feasible here vs fall back to Kaniko."**
- **Cache:** `static-zfs-pv`, pool `zpool5` (→ srvk8s4), `quota=60Gi` (grows later; the wanted
  base-image cache shares it), GC `keepstorage` ≈ 45 Gi (below quota). Deployment + `Recreate`,
  PVC-pinned, perf-taint toleration. Memory + CPU limits as the OOM backstop.
- **Endpoint:** LoadBalancer + `buildkit.home` DNS, mirroring step-ca.
- **Registry push:** `buildkitd.toml` `[registry."registry:5000"] http = true`; the daemon does
  the push.
- **Concurrency:** Jenkins-side semaphore (not a daemon cap). KubeCoder concurrency isn't covered
  by it — the memory limit is the backstop.

## Open decisions (for the next session)

1. **Proxy standard for restricted services:** Envoy + RBAC vs nginx cert-DN. (Not needed for
   BuildKit; needed when the first restricted service lands.)
2. **Cert-please issuance mechanism:** homegrown annotation-driven controller vs adopt
   cert-manager. If homegrown: do **not** co-mint into an ESO-owned Secret (ESO reconciles and
   clobbers foreign keys) — issue into a dedicated Secret; annotate a plain Secret / small CRD,
   not the ExternalSecret.
3. **Principal scheme:** confirm the `spiffe://homelab/...` SAN convention (or simpler CN-based)
   before issuing the first client certs.
4. **Rootless feasibility on srvk8s4** — the gating spike (node prereq above).

## Pointers

- Detailed grounding (file:line): `../AnsibleSpecs/change_requests/buildkit_daemon/design_buildkit.md`
  (operator: `~/source/AnsibleSpecs/...`) — its privileged assumption is superseded by Decision 1.
- Reference issuance patterns: `ansible/roles/internal_tls/` (JWK split-issuance),
  `ansible/roles/microk8s/tasks/internal_tls.yml` (non-HTTP daemon cert precedent),
  `ansible/roles/baseline/` (root-cert distribution).
- Storage pattern: `HelmCharts/terraform-modules/static-zfs-pv/`, KubeCoder's zpool5 invocation.
- Reference write-up: t-velmachos Medium "Build Docker images on K8s faster with BuildKit"
  (privileged + mkcert + registry-cache — we diverge on all three: rootless + step-ca + local cache).

## Recommended follow-up Triage cards (not yet created)

- **Secure `registry:5000`** (TLS + auth; update Kaniko / version-poller / all consumers).
  Ansible-led, bigger slice.
- **General workload cert-issuance ("cert-please") + the proxy/authz pattern.** Platform track;
  decide homegrown vs cert-manager.
