# Add an OIDC client to an app

When to use this runbook: an app's deploy repo gains a Keycloak client, adopts one the realm
already holds, or rotates its client's secret, or the Keycloak provider is bumped. An app's
Keycloak clients belong to its deploy repo's Terraform. They are declared through the app's copy
of the `keycloak-client` module and applied by the app's Argo CD PreSync hook. The clients
adopted so far, each by import, are:

- in the monorepos, pgAdmin's, Guacamole's, Jenkins' and YouTrack's (HomelabAppsDeploy
  `pgadmin/`, `guacamole/`, `jenkins/` and `youtrack/`) and Grafana's (PlatformAddOnsDeploy
  `grafana/`);
- in an app's own deploy repo, at its root, DHCPApp's `dhcp-app` (DnsmasqDeploy),
  ElectronicsInventory's `electronics-inventory`, FieldnotesApp's `fieldnotes-app`,
  ZigbeeControl's `zigbee-control` (Zigbee2mqttDeploy) and MyDownloads' `mydownloads`
  (MediaDeploy).

Copy the one whose case is yours: pgAdmin's, or DnsmasqDeploy's in an app's own repo, for a
secret its own chart reads; Grafana's for one an upstream chart reads through its values; Jenkins'
or YouTrack's for a secret held outside Kubernetes; Guacamole's, or MediaDeploy's in an app's own
repo, for a public client.

Context:

- The design: AnsibleSpecs
  [`handovers/keycloak-access/design-keycloak-client.md`](../../../AnsibleSpecs/handovers/keycloak-access/design-keycloak-client.md).
- The client secret's rotation: secret-rotation R163 and §3.7
  ([`design.md`](../../../AnsibleSpecs/secret-rotation/design.md)). Its leaves and markers:
  [`catalog.md`](../../../AnsibleSpecs/secret-rotation/catalog.md).

**Every commit is planned before it lands.** A stage that tracks `main` applies whatever reaches
it, so a commit that touches an app's clients goes to a topic branch first. It reaches `main` only
after a plan Job has read it at its SHA, for each stage it touches
([`argocd.md`](argocd.md#planning-a-stages-terraform-as-its-hook-applies-it)). Fixes land the same
way. In an app's own deploy repo the app's directory is the whole repo, so every commit there, a
README's included, lands this way, its plan Job with `APP_PATH` empty. Starting the plan Job is a
write, like every push, sync and OpenBao write here: the operator runs it, or says Claude may ([`argocd.md`](argocd.md#conventions)). `$KC` below is that page's
shorthand for the prd-write kubeconfig.

## The convention

- **The module.** An app's directory carries a copy at `terraform/modules/keycloak-client/`, as
  it does each module it uses (AnsibleSpecs `decisions.md`, Tool split). Copy an adopted app's.
  Every copy is byte-identical. Each monorepo's root test asserts its own copies identical, and no
  test compares one repo's copies with another's, so a change to the module is a commit to every
  copy in every deploy repo that carries one. The provider's pin travels with each copy. The
  module's mocked tests run only from the app's test verb, in a copy outside the checkout: copy an
  adopted app's line in its repo's `.kubecoder/project.yaml`, "The keycloak-client module's tests,
  run in a copy outside the checkout".
- **What it declares** is one client and what hangs on it:
  - `keycloak_openid_client`, with `prevent_destroy`, so a change that would replace the client
    fails the plan;
  - a `keycloak_role` per role and a `keycloak_generic_protocol_mapper` per mapper;
  - `keycloak_client_scope_assignments`, only where the call sets `scopes`;
  - a `keycloak_openid_client_service_account_role` per service-account role and one per granted
    role;
  - the `kubernetes_secret_v1`, where the call sets `secret`.

  Nothing realm-wide is declared: no realm, flow, group or user resource and no
  `keycloak_user_roles`. Nor is there a data source on another app's client, which would pull that
  client's secret into this state. A grantee is looked up by username and never managed. A grant
  maps one role to one user, so an assignment the call does not declare is left alone.
- **Its inputs** are in its `variables.tf`. The settings the provider would otherwise reset default
  to the realm's shared baseline, so a call names one only where its client differs.
- **The app names its realm.** Each stage's `config/<stage>/terraform.tfvars` sets
  `keycloak_realm`. The root's `variable "keycloak_realm"` defaults to `"homelab"`, because a
  Destroy Stage plans without the stage's tfvars once `config/<stage>/` is gone. The root test
  `terraform/tests/stage_tfvars.tftest.hcl` plans the root that way, on mock providers, and the
  app's test verb runs it after `tf-check`. Where a name the provider validates derives from
  `var.namespace`, which the sync hook sets, the test sets the namespace too, as YouTrack's and
  FieldnotesDeploy's do for a volume. A stage whose realm holds none of the app's clients
  declares none.
- **The provider block** configures itself from variables and literals alone: a Destroy Stage
  keeps a root's `terraform`, `provider` and `variable` blocks and drops every other block. Every
  adopted app's `terraform/providers.tf` carries it:

  ```hcl
  provider "keycloak" {
    realm            = var.keycloak_realm
    url              = { homelab = var.keycloak_homelab_url }[var.keycloak_realm]
    base_path        = ""
    client_id        = { homelab = var.keycloak_homelab_client_id }[var.keycloak_realm]
    client_secret    = { homelab = var.keycloak_homelab_client_secret }[var.keycloak_realm]
    keycloak_version = "26.7.3"
  }
  ```

  The root declares `keycloak_homelab_url`, `keycloak_homelab_client_id` and
  `keycloak_homelab_client_secret` without defaults, the secret `sensitive`. The hook sets them
  ([§ The hook's credential](#the-hooks-credential)). `keycloak_version` is Keycloak's version as a
  literal: Keycloak withholds its version from an admin client confined to one realm. A bump of
  Keycloak changes it in every adopted app's `providers.tf`. Never configure the provider through
  `KEYCLOAK_*` environment keys, because every app root's bare `provider "keycloak" {}` would read
  them.

## Which secret the app owns

The app's Terraform owns the secret of a client whose secret only Kubernetes reads. The call sets
`secret`, and the module writes the client's id and secret to a Secret in the stage's namespace,
named `keycloak-client`, with keys `client_id` and `client_secret` unless the call names others:
Grafana's names `GF_AUTH_GENERIC_OAUTH_CLIENT_ID` and `GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET`, the
environment keys Grafana reads. A second client in the same namespace takes a name of its own.
The provider reads the secret back as empty where it may not read it, so a precondition fails the
plan on an empty secret.

A secret used outside Kubernetes stays where it is used. The call sets neither `secret` nor the
keeper, `secret_rotation`, since a keeper commit would regenerate the secret and end the copy
held there:

- `jenkins`' in Jenkins' own settings, `youtrack`'s in YouTrack Hub and `kubecoder-mcp`'s in
  claude.ai's connector;
- `iotsupport-pipeline`'s in OpenBao, `jenkins/iotsupport-pipeline-oidc`, for Jenkins, where its
  `keycloak-client` seed entry stays.

A public client (`guacamole`, `mydownloads`) has no secret, and its call sets neither. A keeper
wired later plans nothing while it is never set.

## What stays by hand

| Client or object | Why |
| --- | --- |
| `argocd` | Hand-made by the operator, not Terraform (argo-cd D9); it also keeps Argo CD's sign-in from depending on its own hook |
| `openbao` | Ansible's `roles/openbao` configures OpenBao's side, and its `groups` mapper binds the realm group `openbao-admin`, which is realm-wide |
| `homelab-dev`'s clients | No app deploys a dev stage there: dev stages sign in against `homelab` |
| the `iotdevice-*` clients | IoTSupport makes, rotates and deletes them. The module declares clients by name and never lists the realm's |
| `open-webui` | An orphan, with no app to declare it |
| the built-in clients | Keycloak's own |
| `keycloak-review-readonly` | The adoption's read-only account, temporary |
| `argocd-hook` | The hook's own credential: nothing that runs with it may make it |
| `secret-rotator` | The rotator's own client, made at its go-live (secret-rotation R97) |
| the realm: settings, flows (`direct grant no OTP` among them), groups, users, default scopes, client policies | Nothing realm-wide |

Keycloak's own app, HomelabAppsDeploy `keycloak/`, declares no Keycloak resource
([§ While Keycloak is down](#while-keycloak-is-down)).

## A new client

1. In the app's directory, do as an adopted app does:
   - copy the module, and add its test line to the app's test verb;
   - add the provider block and its four variables;
   - set `keycloak_realm` in each stage's `config/<stage>/terraform.tfvars`;
   - add the root test, and its line in the test verb after `tf-check`,
     `cexec iac terraform -chdir=terraform test -no-color`.

   Where the app owns the secret, a root without `variable "rotation_epoch"` declares it as
   pgAdmin's and Grafana's `terraform/main.tf` do, and each stage gets a
   `config/<stage>/rotation.tfvars` that reads `rotation_epoch = {}`.
2. The call, `module "keycloak_client"` for an app's one client: the client, its roles, mappers,
   grants and service-account roles, and, where the app owns the secret
   ([§ Which secret the app owns](#which-secret-the-app-owns)), `secret` and
   `secret_rotation = lookup(var.rotation_epoch, "keycloak_client", null)`. A keeper is named
   after the label of the block it re-mints, so a second client's call takes a label and a keeper
   of its own. Leave `scopes` null, so the new client gets the realm's default scopes.
3. The chart reads the Secret by name: pgAdmin's through `secretKeyRef` `name: keycloak-client`,
   Grafana's upstream chart through `envFromSecret: keycloak-client` in its stage values, which
   takes every key. The hook writes it before the sync phase starts the pods.
4. Run the app's test verb.
5. Push a topic branch, then plan each stage it touches at its SHA. The client, each role, mapper,
   grant and service-account role, and the Secret read `create`, and nothing else reads anything.
   Then `main` takes that SHA, the stage syncs, and the topic branch is deleted.
6. Run [§ After the sync](#after-the-sync)'s token check, and add
   [§ Rotating the client secret](#rotating-the-client-secret)'s marker and token repository.

## Adopting an existing client

The client already exists in the realm. It is imported, never recreated: the import commit plans
imports alone, and the commit after it drops the import blocks. Where the app owns the secret, that
commit also takes it over from the ExternalSecret and the OpenBao leaf the app read it from. Each
adoption so far ran this way on 2026-11-21, the import commit first:

- pgAdmin's in HomelabAppsDeploy `1c6efc6` and `737fd52`, Guacamole's in `c63cd5f` and `4a3919c`,
  and Jenkins' and YouTrack's together in `42c6e64` and `221d5ba`;
- Grafana's in PlatformAddOnsDeploy `bb0cb74` and `d6cd7cb`;
- DHCPApp's in DnsmasqDeploy `aa30b54` and `240c6c2`, ElectronicsInventory's in
  ElectronicsInventoryDeploy `dc908b4` and `3ba9eea`, FieldnotesApp's in FieldnotesDeploy
  `1274470` and `9783bcf`, ZigbeeControl's in Zigbee2mqttDeploy `611dcc3` and `3e6cf49`, and
  MyDownloads' in MediaDeploy `b0653f0` and `184e0f4`.

### 1. The fresh read

Read the app's clients through `keycloak-review-readonly`, the realm's read-only service-account
client (realm-management's view roles alone). Use AnsibleSpecs
`handovers/keycloak-access/tools/keycloak_dump.py`, which only GETs and calls no secret endpoint.
It reads the account's secret from `kv/shared/keycloak-review/<realm>` into its token request and
nowhere else. An agent reads that leaf only with the operator's permission for that path. Keep the
output out of every repository, in `/tmp`. From `/work/Ansible`, in bash, since the login script
finds the repo root through bash's `BASH_SOURCE`:

```sh
bash -c '. scripts/bao-login.sh && python3 -I ../AnsibleSpecs/handovers/keycloak-access/tools/keycloak_dump.py homelab /tmp/<dir>'
```

Diff the read against the review's dump,
`handovers/keycloak-access/dump-2026-11-20/<realm>/clients/<client>.json`. Anything that changed
since 2026-11-20 is restated in the call as it now is, and reported. The read also gives the ids
the import blocks name: the client's, each role's and mapper's, and each grantee's user id. For a
service-account client it also gives its service account user's id and the id of each other
client whose role it holds.

A mapper's config is restated as the mapper's own endpoint returns it,
`/admin/realms/<realm>/clients/<client uuid>/protocol-mappers/models/<mapper uuid>`, which is what
the provider reads. The client's representation, which the dump writes, can leave keys out: it
lacked `"userinfo.token.claim": "false"` on DHCPApp's, ElectronicsInventory's and ZigbeeControl's
audience mappers, and a call restated from it plans the mapper `update, import`. GET each mapper
through the same account.

### 2. The import commit, on a topic branch

The commit holds:

- where the app has no client yet, the root's setup of [§ A new client](#a-new-client) step 1:
  the module copy and its test line, the provider block and its variables, `keycloak_realm` in
  each stage's tfvars, the root test, and, where the app owns the secret, the keeper variable and
  file;
- the module call, restating the client whole from the fresh read, every field the module
  models, so that a later commit's diff is exactly its change;
- its grants and service-account roles;
- one `import {}` block per resource, with literal ids, in `terraform/imports.tf`;
- where the app owns the secret, `secret_rotation` wired as above, with no keeper in
  `rotation.tfvars`. It is then null, so nothing re-mints the secret;
- no `secret` yet;
- in the root test, an `override_resource` for each imported resource, since a mock provider
  cannot import. Each targets the resource without its key, as in
  `module.keycloak_client.keycloak_role.this`.

| Resource (under `module.keycloak_client.`) | Import `id` |
| --- | --- |
| `keycloak_openid_client.this` | `<realm>/<client uuid>` |
| `keycloak_role.this["<role>"]` | `<realm>/<role uuid>` |
| `keycloak_generic_protocol_mapper.this["<mapper>"]` | `<realm>/client/<client uuid>/<mapper uuid>` |
| `keycloak_openid_client_service_account_role.grant["<username>/<role>"]` | `<realm>/<grantee's user uuid>/<client uuid>/<role uuid>` |
| `keycloak_openid_client_service_account_role.service_account["<key>"]` | `<realm>/<service account user uuid>/<uuid of the role's client>/<role uuid>` |
| `keycloak_client_scope_assignments.this[0]` | `client/<realm>/<client uuid>` |

What bites on import, and how the call meets it:

- **Settings the provider resets.** Some have a default and are sent on every update: `enabled`,
  `client_authenticator_type`, `full_scope_allowed`, `use_refresh_tokens`,
  `use_refresh_tokens_client_credentials`, `standard_token_exchange_enabled`,
  `backchannel_logout_session_required`, `oauth2_device_authorization_grant_enabled` and
  `always_display_in_console`. Others are reset where the live value is not zero:
  `pkce_code_challenge_method`, `backchannel_logout_revoke_offline_sessions` and the flow
  overrides. The call names each one where the fresh read differs from the module's default.
- **Kept as Keycloak holds them.** The provider's update never sends the attributes it does not
  model, such as CIBA, `frontchannel.logout.session.required` and `logout.confirmation.enabled`.
  `extra_config` stays empty.
- **The `authorization` block** stays out, because no client uses authorization services. Declared,
  it would switch them on. Left out, an update switches them off, so a client that uses them would
  import as `update, import`, and the gate stops it.
- **A flow override** is restated with each key the client binds, each flow's id from the fresh
  read. Left out, the plan removes the override. The module writes both keys, so one left null
  clears that override. `mydownloads` binds `direct_grant` alone, to the realm's `direct grant no
  OTP`. The flow stays the realm's.
- **URL lists the provider refuses.** It refuses redirect URIs on a client with neither the
  standard nor the implicit flow, and web origins on one that also has direct access grants off.
  It checks only on a create or an update, so the import restates them and plans zero. The
  console's default `/*` stands in both lists of a client whose one flow is service accounts, and
  `/*` stood in the redirect URIs of `mydownloads`, on direct access grants alone. The commit after
  the import sets the refused lists empty. Until that commit lands, no change to the client can
  apply.
- **Scope assignments are authoritative.** `keycloak_client_scope_assignments` detaches whatever
  its two lists leave out, built-ins included. A create on an existing client does so without
  showing it. So a call sets `scopes` only where its app names its scopes, writes both lists whole
  from the fresh read, and imports them. Everywhere else, a client's scopes stay as they are.
- **Mappers** are the generic `keycloak_generic_protocol_mapper`, with Keycloak's config exactly. A
  typed mapper would drop keys such as `lightweight.claim` on its first update. A generic mapper's
  name cannot change in place, so the call keys each mapper by its live name.
- **The secret** is read into state on import and stays out of the config. With no keeper set,
  nothing re-mints it: the import plans no change on it, and no `client_secret` is known only
  after apply.

### 3. The gate

Plan each stage the commit touches at the topic branch's SHA. The commit lands only if every line
of the change list reads `<address>: import`. An import that would also change something reads
`update, import`. That line, a create, or a plan that fails stops the adoption, and it comes to
the operator. pgAdmin's plan read `presync: the plan carries 4 change(s):`, then the client, the
role, the mapper and the grant, each `: import`, and Terraform's `Plan: 4 to import, 0 to add, 0
to change, 0 to destroy.`

### 4. `main` and the sync

`main` fast-forwards to the SHA the plan read. Where origin's `main` has moved, take it onto the
branch and plan again. The stage syncs, its hook's log reads `Apply complete! Resources: <n>
imported, 0 added, 0 changed, 0 destroyed.`, and the app's pod does not restart. A plan Job at
`main` then reads `presync: the plan carries no changes`. Delete the topic branch. Until the
takeover, a later commit lands only on a plan that carries no changes.

### 5. The commit after the import

The commit holds exactly the following, and its plan lists exactly their changes. Anything else
stops the adoption.

- for a client whose secret the app owns, the takeover (below);
- the URL lists the provider refuses set empty: both on a service-account-only client, the
  redirect URIs alone on one on direct access grants alone, whose web origins pass;
- `imports.tf` dropped, with the root test's overrides.

A takeover's plan reads `module.keycloak_client.kubernetes_secret_v1.this[0]: create` alone.
MyDownloads' read `module.keycloak_client.keycloak_openid_client.this: update` alone, its diff
`valid_redirect_uris` alone. A commit that only drops the import blocks plans `presync: the plan
carries no changes`, and the adoption ends at its sync.

**The takeover.**

- The call sets `secret = { name = "keycloak-client", namespace = var.namespace }`, with
  `client_id_key` and `client_secret_key` where the app reads other key names, as Grafana's does.
  The name differs from the ExternalSecret's target (pgAdmin's was `pgadmin-oidc`, Grafana's
  `grafana-oidc`), so the PreSync apply can create the Secret while the ESO-owned one still exists.
- The app's references switch to `keycloak-client`, which rolls the pods: pgAdmin's chart's
  `secretKeyRef`s, Grafana's `envFromSecret`. Where a template falls back to chart values while
  the ExternalSecret's entry is absent, as DnsmasqDeploy's, ElectronicsInventoryDeploy's and
  Zigbee2mqttDeploy's did, the fallback and its `oidc.clientID` and `oidc.clientSecret` defaults
  go too: dropping the entry alone renders both variables empty.
- The ExternalSecret leaves the chart: pgAdmin's stage values, Grafana's companion chart
  (`chart/templates/stage-manifests.yaml`). Argo CD prunes it in the sync phase, after the hook,
  and ESO's owner reference takes its Secret with it.

In pgAdmin's sync, the hook wrote the Secret at 09:00:26Z, before the new pod started at 09:00:33Z.
Afterwards the namespace held `keycloak-client`, with no owner, and no `pgadmin-oidc`.

### After the sync

1. **The token check**, from inside the cluster, with the credentials as the app's pod holds them.
   For a client on the code flow, the script posts them with a dummy code: `400 invalid_grant`
   means the client authenticated, and `401 unauthorized_client` means Keycloak refused the
   secret. A service-account client posts `grant_type` `client_credentials` with no `code`, and
   answers `200`. The script prints the status and the `error` alone, never the secret.
   pgAdmin's `pgadmin-app` container holds the credentials in `OIDC_CLIENT_ID` and
   `OIDC_CLIENT_SECRET`:

   ```sh
   cexec iac kubectl $KC exec -i -n pgadmin-prd deploy/pgadmin -c pgadmin-app -- /venv/bin/python3 - <<'EOF'
   import json, os, urllib.error, urllib.parse, urllib.request
   url = "https://auth.ginbov.nl/realms/homelab/protocol/openid-connect/token"
   form = urllib.parse.urlencode({"grant_type": "authorization_code", "code": "dummy",
                                  "client_id": os.environ["OIDC_CLIENT_ID"],
                                  "client_secret": os.environ["OIDC_CLIENT_SECRET"]}).encode()
   try:
       with urllib.request.urlopen(urllib.request.Request(url, data=form), timeout=30) as resp:
           print(resp.status)
   except urllib.error.HTTPError as e:
       print(e.code, json.load(e).get("error"))
   EOF
   ```

   A container without Python makes the same request with curl, each secret on stdin
   (`--data-urlencode client_secret@-`), printing only the status and the `error`. Grafana's
   `grafana` container holds the credentials in `GF_AUTH_GENERIC_OAUTH_CLIENT_ID` and
   `GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET`. The containers below hold them in `OIDC_CLIENT_ID` and
   `OIDC_CLIENT_SECRET`:

   | App | Deployment, container | Python |
   | --- | --- | --- |
   | DHCPApp | `dnsmasq-prd` `dhcp-app`, `dhcpapp-app` | `/app/.venv/bin/python3` |
   | ElectronicsInventory | `electronics-inventory-prd` `electronics-inventory`, `electronics-inventory-app` | `python3` |
   | FieldnotesApp | `fieldnotes-prd` `fieldnotes`, `app` | `python` |
   | ZigbeeControl | `zigbee2mqtt-prd` `zigbee-control`, `zigbee-control-app` | `/app/.venv/bin/python3` |

   After a takeover, run it in the new pod by name, once `kubectl rollout status` returns: the old
   pod, still Terminating then, holds the ESO Secret's copy in its environment.

   A public client on the password grant, `mydownloads`, posts `grant_type` `password` with its
   `client_id` and a username and password no user holds. `400 invalid_grant` means Keycloak took
   the client and refused the user; a client with direct access grants off answers `400
   unauthorized_client`. Only a user's own sign-in shows such a client's token.

2. **The old leaf.** Once the check holds and the ExternalSecret and its Secret are gone, check that
   no ExternalSecret and no deploy-repo file names the leaf, then delete it whole, with its
   versions:

   ```sh
   cexec iac kubectl $KC get externalsecrets -A -o json \
     | jq -r --arg l "<leaf>" '.items[] | select(tostring | contains($l)) | .metadata.namespace + "/" + .metadata.name'
   . scripts/bao-login.sh
   cexec iac bao kv metadata delete -mount=kv <leaf>
   cexec iac bao kv metadata get -mount=kv <leaf>
   ```

   **Reading:** no ExternalSecret line, then `No value found at kv/metadata/<leaf>`.
3. **SecretRotator and the catalog.** The leaf's entry leaves SecretRotator's
   `src/secret_rotator/seed.yaml`, and `store-keys.json` and the `keycloak-client` seed-row tests
   (`tests/test_keycloak_client.py`) follow. The catalog marks the leaf's row deleted, and the
   client's marker comes in where the kind can rotate the app's stage
   ([§ Rotating the client secret](#rotating-the-client-secret)). The
   [go-live runbook](secret-rotator-go-live.md)'s `keycloak-client` loops drop the leaf, and the
   marker joins its wave 4 lists and the stagger of its item 13.
4. **The sign-in.** The operator signs in to the app through Keycloak once.

## A change to an adopted client

A change to the client, its roles, mappers or grants is a commit to the call, planned at its SHA
on a topic branch before `main` takes it, as every commit is. FieldnotesApp's web origins losing
their trailing slash, FieldnotesDeploy `ccbfb29`, planned
`module.keycloak_client.keycloak_openid_client.this: update` alone, its diff `web_origins` alone.
With no keeper changed the secret is not regenerated, so the Secret and the app's pods are left
alone.

A fresh read after the sync, diffed against one from before, shows the change. On a client's
first update it also shows boolean attributes the client lacked, which the provider sends on every
update: `fieldnotes-app` gained `use.refresh.tokens` at `"true"`,
`client_credentials.use_refresh_token` at `"false"`, and `exclude.issuer.from.auth.response` and
`exclude.session.state.from.auth.response` at `"false"`, and `mydownloads` the last two. Each is
the value Keycloak 26.7.3 applies to the absent key. `post.logout.redirect.uris` can come back in
another order.

## Rotating the client secret

A rotation is a keeper commit, the `terraform` kind's (secret-rotation R163).

- `[<app>/]config/<stage>/rotation.tfvars` maps the keeper, `keycloak_client` for an app's one
  client, to a value. A new value regenerates the secret in the hook's apply, which rewrites the
  Secret in the same apply. Then the Secret's readers restart. A keeper once set is never removed,
  because dropping it re-mints the secret too.
- Keycloak ends the old secret as it makes the new one, so the app's sign-in fails from the hook's
  apply until its pods restart.
- Each owned client has a marker in SecretRotator's seed,
  `rotator/terraform/<app>-<stage>/<keeper>`:
  - key `client_secret`, 14 d, `activate: none`;
  - its args: the repo, the app's directory (`""` at a repo's root), the Application, the keeper
    and the Secret.

  pgAdmin's is `rotator/terraform/pgadmin-prd/keycloak_client`, and DHCPApp's, at DnsmasqDeploy's
  root, `rotator/terraform/dnsmasq-prd/keycloak_client`. The client's repo is on the kind's
  token, whose repositories `rotator/terraform/credentials`' notes list. The seed refuses notes
  longer than 512 bytes on a leaf with an `expires_at`, and these notes are near that limit, so a
  repo added to them needs shorter wording.
- The kind reads the hook's parameters from the Application's `spec.source`, which a multi-source
  Application lacks. So Grafana's client, behind the multi-source `grafana-prd`, has no marker yet,
  and rotates by a hand keeper commit.
- The kind refuses an Application that tracks a branch other than `main`. A stage that reaches prd
  only by promotion rotates by a hand keeper commit and a promotion.

**By hand, until SecretRotator's `terraform` kind is enabled and holds the client's marker.** It
is the operator's call: nobody makes one unasked.

1. On a topic branch, commit the keeper's new value in the form the kind writes, a UTC timestamp,
   in `rotation_epoch = { "keycloak_client" = "<YYYY-MM-DDTHH:MM:SSZ>" }`.
2. Its plan Job reads an in-place update of the client and one of the Secret, and nothing else.
3. `main` takes that SHA, the stage syncs, and the hook re-mints the secret.
4. Restart the Secret's readers at once: the Deployments the secret-rotation catalog's `terraform`
   marker table lists for the client's marker, and Grafana's, which has none. pgAdmin's and
   Grafana's:

   ```sh
   cexec iac kubectl $KC rollout restart -n pgadmin-prd deployment/pgadmin
   cexec iac kubectl $KC rollout restart -n grafana-prd deployment/grafana
   ```

5. Run [§ After the sync](#after-the-sync)'s token check.

## The hook's credential

- **The client.** `argocd-hook` in `homelab`: confidential, with service accounts as its one flow.
  Its service account holds realm-management's `manage-clients`, `manage-users` and `view-users`.
  The operator made it in the admin console, and no Terraform declares it.
- **Its leaf** is `eso/prd/argocd-hooks/keycloak/homelab`, with `client_id` and `client_secret`,
  under the `eso` AppRole's `eso/prd/*`. ArgoCDDeploy's `hooks.environment`
  (`config/prd/values.yaml`) puts it in `argocd-hooks/argocd-hook-credentials` as
  `TF_VAR_keycloak_homelab_client_id` and `TF_VAR_keycloak_homelab_client_secret`. Beside them is
  the literal `TF_VAR_keycloak_homelab_url`, `https://auth.ginbov.nl`, the bare origin. Every hook
  Job, plan Job and Destroy Stage Job reads the Secret through `envFrom`, and Terraform ignores a
  `TF_VAR_` key that no root declares.
- **A second realm** takes a client and leaf of its own, `eso/prd/argocd-hooks/keycloak/<realm>`.
  It also takes:
  - its three `TF_VAR_keycloak_<realm>_*` keys in `hooks.environment`;
  - an entry in ArgoCDDeploy `tests/render-chart.py`'s `HOOK_KEYCLOAK_REALMS`. The render test
    asserts the key set exactly, and it refuses a `KEYCLOAK_*` key;
  - a key in the provider block's maps in each app that names the realm.
- **Its rotation.** Its seed entry is `keycloak-client` with
  `{"realm":"homelab","client":"argocd-hook"}` at 14 d and `activate: eso`. Once SecretRotator's
  `keycloak-client` kind is enabled, the entry regenerates the secret, writes the leaf and syncs
  `argocd-hook-credentials`, and it restarts nothing. Until then the operator rotates it by hand, when needed:

  1. In the admin console, open `homelab`, then Clients, `argocd-hook` and Credentials.
     Regenerate the secret and copy it.
  2. Patch the leaf from a hidden prompt:

     ```sh
     . scripts/bao-login.sh
     read -rs tok && printf %s "$tok" | cexec iac bao kv patch -mount=kv eso/prd/argocd-hooks/keycloak/homelab client_secret=-; unset tok
     ```

  3. Force-sync the ExternalSecret:

     ```sh
     cexec iac kubectl $KC annotate externalsecret -n argocd-hooks argocd-hook-credentials force-sync=$(date +%s) --overwrite
     ```

  A hook run between steps 1 and 3 fails its Keycloak login, and Argo retries it: four runs over
  about 12 minutes ([`argocd.md`](argocd.md#diagnosing-a-failed-sync)). An app whose retries ran
  out is synced again by hand. **Reading:** a plan Job of an adopted app at its `main` reads
  `presync: the plan carries no changes`.
- **Keycloak 26.8.2 and later** make the provider's admin client also need the `kc-admin-api`
  scope. The upgrade to 26.8.2 creates the scope, so `argocd-hook` takes it right after that roll,
  and until it has it every adopted app's hook fails. The SOP for bumping Keycloak's version
  carries the step.

## The provider's version

- It is pinned exactly in the module's own `versions.tf`, `version = "5.11.0"`.
- It comes from the public registry, as `hashicorp/kubernetes` and `cyrilgdn/postgresql` do,
  because the hook's `terraform.rc` mirrors only `pvginkel/*`.
- No lock file is committed: every deploy repo gitignores `.terraform.lock.hcl`.
- **A bump** is one commit to every copy of the module, in every deploy repo that carries one. It
  lands only once a plan Job at its SHA reads `presync: the plan carries no changes` on every
  stage of every adopted app.

## While Keycloak is down

- **An adopted app waits.** Every adopted app's hook fails its refresh, so the app's sync waits for
  Keycloak, a chart-only change's included, and so does its plan Job. Once Keycloak answers, a sync
  whose retries ran out is synced again by hand, since Argo does not retry a failed revision
  ([`argocd.md`](argocd.md#diagnosing-a-failed-sync); [`cold-boot.md`](cold-boot.md) step 8).
- **Keycloak never waits on itself.** Keycloak's own app declares no Keycloak resource. No app
  Keycloak's cold start rests on declares a client either: the shared Postgres cluster, the
  registry, storage, nginx, OpenBao and ESO, and Argo CD. `argocd` and `openbao` stay by hand.
- **dnsmasq waits only to deploy.** DnsmasqDeploy declares DHCPApp's client, and dnsmasq serves
  `.home` DNS and DHCP. On a cold boot its pods come back without a sync, and Keycloak's needs
  resolve through names CoreDNS pins. So while Keycloak is down DNS and DHCP serve, and only a
  DnsmasqDeploy change waits. After a full cluster loss its hand render carries no hook, so
  `dhcp-app` starts only once the hook has written `keycloak-client`
  ([`cluster-bootstrap.md`](cluster-bootstrap.md) step 10).
- **A Destroy Stage** of an adopted app deletes its clients, on the plan the operator confirms. Its
  reduced root binds no `prevent_destroy`
  ([`argocd.md`](argocd.md#destroying-a-retired-stage)).
