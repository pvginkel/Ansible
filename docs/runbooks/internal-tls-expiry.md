# Runbook: expired internal_tls leaf certificate

The X.509 counterpart of [`ssh-host-cert-expiry.md`](ssh-host-cert-expiry.md).

Commands run from the Ansible checkout, as in that runbook. In a KubeCoder environment the toolchain
lives in the `iac` sidecar, so put `cexec iac` after the `cd`: `cd ansible && cexec iac poetry run …`
(see [`../live-infra-access.md`](../live-infra-access.md)).

## Symptom

An `internal_tls` leaf is the homelab-CA certificate a service presents, issued by step-ca through
the `internal_tls` role. Once it lapses, every client that verifies it refuses the connection. Go
clients (`bao`, `kubectl`, `step`) print:

```
tls: failed to verify certificate: x509: certificate has expired or is not yet valid: current time … is after …
```

curl prints `SSL certificate problem: certificate has expired`. Python clients print
`certificate verify failed: certificate has expired`.

| Consumer | Where the leaf is served | Who notices |
|---|---|---|
| Proxmox web UI (`proxmox_host`) | `https://<node>.home:8006` on pve, pve1 and pve2, from `/etc/pve/local/pveproxy-ssl.pem`. Each node has its own leaf. | Browsers and API clients of that node. |
| `kubernetes-api.home` (`microk8s`) | `https://kubernetes-api.home:16443`, the keepalived VIP over srvk8s1–3. On srvk8sdev the name is `kubernetes-api-dev.home`. The leaf is `/var/snap/microk8s/current/certs/homelab-api.crt`. srvk8s4 is worker-only and has none. | Only clients that connect by that name. The apiserver serves the leaf only to SNI matching its SANs; kubelet, in-cluster traffic and clients using an IP or another name get microk8s's own `server.crt`. A lapsed leaf on a node not holding the VIP stays hidden until the VIP moves there. |
| OpenBao listener (`openbao`) | `https://secrets.home` (443, passed through to 8200) and `https://srvvaultN.home:8200` on srvvault1–3, from `/etc/openbao/tls/tls.crt`. | Every OpenBao client: `bao`, the web UI, ESO, Jenkins `withVault`, and every `iac` run. **This breaks the usual recovery path** — see [The OpenBao listener leaf](#the-openbao-listener-leaf-openbao-is-unreachable). |

## Cause

Leaves live **47 days**, and `internal_tls` re-issues a leaf once fewer than **14** days are left.
`IaC/Scheduled Certs` (Fridays, `Jenkinsfile.iac-scheduled-certs`) runs
`playbooks/renew-internal-tls.yml` over every leaf, which gives two attempts inside that window. The
daily `iac-scheduled-drift` build reds once a leaf is under 7 days. So a lapse means the Friday job
failed or did not run on two Fridays in a row — **check it before doing anything else**. The build
description says which stage broke:

- `host certs may lapse; TLS leaf renewal did not run` — the SSH host-cert stage failed first, so
  the TLS stage never ran. Step 1 below matters most here. An expired OpenBao listener leaf gives
  this description too, because every stage fails at its first `iac -c`.
- `internal_tls leaves may lapse` — the TLS stage itself failed; its console names the host and task.

## 1 — Confirm the host is reachable over SSH

The renewal runs over Ansible's SSH, so an expired SSH host certificate fails it before any
certificate task. The host-cert stage runs first, and SSH host certificates also last 47 days, so
a lapsed leaf often comes with a lapsed host certificate.

```sh
cd ansible && poetry run ansible 'proxmox:openbao:k8s' -m ping
```

- `pong` — go on.
- `UNREACHABLE` with `Certificate invalid: expired` — fix the host certificate first with
  [`ssh-host-cert-expiry.md`](ssh-host-cert-expiry.md), then come back. Its fix covers only VMs
  Terraform builds from scratch, not pve, pve1 or pve2.
- srvk8sdev is powered off most of the time; from srviac, where the Friday job runs, a connection
  timeout there only means it is off. A KubeCoder environment cannot reach srvk8sdev at all
  ([`../live-infra-access.md`](../live-infra-access.md)), so from there a failure says nothing
  about the box; renew its leaf from srviac.

## 2 — Confirm the leaf has lapsed (read-only)

Read the certificate the service is actually serving. This works on an expired leaf, and needs no
login:

```sh
openssl s_client -connect pve1.home:8006 -servername pve1.home </dev/null 2>/dev/null \
    | openssl x509 -noout -subject -enddate
```

Use the same pair of options for the other consumers:

| Consumer | `-connect` | `-servername` |
|---|---|---|
| Proxmox | `<node>.home:8006` | `<node>.home` |
| kube-apiserver | `srvk8sN.home:16443` | `kubernetes-api.home` (on srvk8sdev: `kubernetes-api-dev.home`) — without it the apiserver serves microk8s's own certificate |
| OpenBao | `srvvaultN.home:8200` | `secrets.home` |

A `notAfter` in the past means the leaf has lapsed. One inside 14 days is due, and the next renewal
run issues it.

## 3 — Renew

Once the host is reachable, a plain renewal run recovers the lapsed leaf. If the lapsed leaf is
OpenBao's, use [the section below](#the-openbao-listener-leaf-openbao-is-unreachable) instead.

```sh
cd ansible && poetry run ansible-playbook playbooks/renew-internal-tls.yml --limit <host>
```

Starting an `IaC/Scheduled Certs` build does the same across the fleet, once its host-cert stage
passes.

**Why a plain run is enough.** The role issues a new leaf when `step certificate needs-renewal
--expires-in 336h` exits 0, and that command also exits 0 for a leaf that has already expired.
Issuance is a fresh `step ca certificate` authorised by a JWK token the controller mints. The old
leaf is not needed, so its expiry does not stand in the way.

**What the run needs.**
- step-ca at `https://ca.home`: the controller mints the token there, and the host fetches its leaf
  there. `curl https://ca.home/health` answers `{"status":"ok"}`. step-ca runs on the prd Kubernetes
  cluster and needs nothing from OpenBao.
- The ansible-vault passphrase: the JWK provisioner password is vaulted in
  `inventories/prd/group_vars/all/vips.yml`.
- The `step` CLI on the controller.

**The playbook's two plays.**
- The first play renews the pveproxy and OpenBao leaves, all hosts at once.
- The last play renews the k8s leaves under `serial: 1`. It stops at the first k8s node that fails
  or is unreachable, and the nodes after it are not reached. `--limit <node>` reaches a lapsed leaf
  on one of those.
- If every proxmox and openbao host fails, the k8s play never runs; `--limit k8s` runs it on its own.
- srvk8sdev takes `--limit k8s_dev`.

**What a good run shows.** The host reports `changed` on `Issue the leaf certificate on the target
host`, then its handler runs: `Reload pveproxy`, `Reload openbao` or `Restart microk8s kubelite`.
Repeat step 2; `notAfter` is now about 47 days out.

A k8s node's `Restart microk8s kubelite` waits for the node to come back ready. If it fails with
`kubelite restarted, but got no … within …s`, the new leaf is already on disk; the node's readiness
is what failed, not the certificate.

Then find out why the Friday job did not renew it, or you will be back here in 47 days.

## The OpenBao listener leaf: OpenBao is unreachable

Every `iac` run logs in to OpenBao before anything else, to resolve the `!bao` refs in srviac's
`/etc/iac/secrets.yaml`. A lapsed listener leaf fails that login, and the container exits:

```
iac-impl: OpenBao AppRole login failed: … certificate verify failed: certificate has expired …
```

That stops every stage of `IaC/Scheduled Certs`, and any hand `iac -c` on srviac. Until this leaf is
renewed, **nothing else in the fleet renews either**, so renew it first.

The renewal itself reads nothing from OpenBao. Only the controller's credentials can depend on it.
Besides step-ca and the `step` CLI, the controller needs two things:
- the `ansible` SSH key, which reaches srvvault1–3;
- the ansible-vault passphrase.

On srviac the SSH key is a `!bao` ref (`kv/iac/ansible-ssh-key#private`); the vault passphrase is a
literal. Two controllers have both without asking OpenBao:

**A KubeCoder environment for this repo whose keys are already on disk.** Its setup
(`scripts/kubecoder-keys.sh`) wrote them to `~/.ssh/id_ed25519_ansible` and `~/.ansible/vault-pass`,
and the environment points `ANSIBLE_VAULT_PASSWORD_FILE` at the second file. The KubeCoder secret
catalog is itself filled from OpenBao (`kv/eso/prd/kubecoder/prd/catalog`, per
`.kubecoder/config.yaml`), so use an environment that already has both files. Don't set up a new one
during the outage.

```sh
ls ~/.ssh/id_ed25519_ansible ~/.ansible/vault-pass
cd ansible && cexec iac poetry run ansible-playbook playbooks/renew-internal-tls.yml --limit openbao
```

On 2026-09-14 such an environment reached pve, pve1, pve2, srvvault1–3 and srvk8s1–4 over SSH
with those keys, and `https://ca.home/health` answered.

**srviac, under the cold-boot procedure.** Follow [`iac-cold-boot.md`](iac-cold-boot.md) steps 1–4.
They replace every `!bao` ref with its Roboform literal, the SSH key included. With no `!bao` ref
left, `iac-impl` makes no OpenBao login. Then:

```sh
sudo iac -c 'cd /work/Ansible/ansible && ansible-playbook playbooks/renew-internal-tls.yml --limit openbao'
```

**After either run:**
1. `Reload openbao` sends SIGHUP to one peer at a time. Repeat step 2 against srvvault1–3 and
   `secrets.home:443`.
2. If you used srviac's cold boot, do [`iac-cold-boot.md`](iac-cold-boot.md) step 5 now that
   OpenBao answers.
3. Start an `IaC/Scheduled Certs` build. Every build since the leaf lapsed failed before renewing
   anything, so SSH host certificates and the other leaves may be due or lapsed as well. Run step 1
   against the fleet first.
4. What ESO and Jenkins did while OpenBao was unreachable, and how to bridge them, is in
   [`openbao.md`](openbao.md) "Consumer cold-boot".

## Notes

- **`--check` previews but cannot fix.** Issuance is a `command`, which check mode skips. A
  `--check` run reports `<cert path> needs (re)issuing — missing=…, within renewal window=…,
  SAN drift=…` as changed for each due leaf, and issues nothing.
- **A lapsed leaf is served, but the file on disk is fresh.** Step 2 shows the lapsed leaf, but a
  `--check` run reports nothing due for that host. The service never reloaded the new leaf,
  typically because a run was killed between issuing and its handler. A plain run finds nothing
  due and reloads nothing. Force a new leaf, and the reload with it, by adding
  `-e internal_tls_renewal_threshold_days=48` to the `--limit <host>` run. Above the 47-day life,
  every leaf in scope counts as due.
- **srvk8sdev** is off more than it is on, and its leaf lapses while it is off. That is the accepted
  cost `Jenkinsfile.iac-scheduled-certs` records. Renew it with `--limit k8s_dev` when it is back.
- Renewal design: the header of `ansible/playbooks/renew-internal-tls.yml` and
  `ansible/roles/internal_tls/README.md`.
