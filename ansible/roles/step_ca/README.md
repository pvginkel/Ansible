# `step_ca` role

Writes step-ca's five Secrets in `step-ca-prd` on the prd cluster from this role's files: the CA's
two encrypted keys and their passwords, its certificates, `ca.json` and `defaults.json`. The
upstream `step-certificates` chart mounts them in `existingSecrets` mode (StepCaDeploy
`config/prd/values.yaml`); StepCaDeploy renders none of them. Design: `/work/AnsibleSpecs/decisions.md`
§"Internal TLS / homelab CA", "Intermediate key + passphrase".

Applied only by [`playbooks/step-ca.yml`](../../playbooks/step-ca.yml), which elects the microk8s
primary among `k8s_prd` (the `microk8s` role's `elect-primary` tasks) and runs the role there with
the node kubeconfig, the one credential a fresh bring-up already has. No Jenkins job runs it; the
operator does, from `ansible/`:

```sh
ansible-playbook playbooks/step-ca.yml
```

It needs the ansible-vault passphrase and the cluster, nothing from OpenBao, so cluster bring-up
runs it before step-ca is first applied ([`cluster-bootstrap.md`](../../../docs/runbooks/cluster-bootstrap.md)
§Before you start).

## Files

`vars/main.yml` `step_ca_secrets` maps each Secret key to the file whose bytes it carries, exactly,
trailing newline or not; the same map is the table in
[`step-ca-bootstrap.md`](../../../docs/runbooks/step-ca-bootstrap.md) day-zero step 7.

- **Vaulted whole-file:** `ca.json`, `defaults.json`, `intermediate_ca_key`,
  `intermediate_ca_password`, `ssh_host_ca_key`, `ssh_host_ca_password`.
- **Plain:** `intermediate_ca.crt` and `ssh_host_ca_key.pub`.
- **The root** is not here: `step-ca-certs`' `root_ca.crt` is `baseline`'s
  `roles/baseline/files/homelab-root.crt`, so step-ca's `/roots.pem` serves the root every managed
  host trusts, and an edit to that file reaches step-ca at the next run
  ([`step-ca-root-rotation.md`](../../../docs/runbooks/step-ca-root-rotation.md)).

Changing the CA's material is replacing a file — a vaulted one whole-file with `ansible-vault` —
and running the playbook. The procedures are in
[`step-ca-bootstrap.md`](../../../docs/runbooks/step-ca-bootstrap.md) and
[`ssh-host-ca-rotation.md`](../../../docs/runbooks/ssh-host-ca-rotation.md). A vaulted file
re-encrypted over the same plaintext changes nothing: the run compares decrypted data.

## What a run does

1. Creates Namespace `step-ca-prd` only when it is missing, the bring-up case. An existing one is
   StepCaDeploy's and stays Argo's.
2. Replaces each Secret whole (`force: true`), after reading the live one. The definition is the
   whole object, so neither Argo's `argocd.argoproj.io/tracking-id` annotation nor kubectl's
   `last-applied-configuration` survives, and Argo does not own the Secrets.
3. Restarts StatefulSet `step-ca` (the pod template's `kubectl.kubernetes.io/restartedAt`) and
   waits up to 300 s for it, when any Secret's data was missing or differed from what the run
   read. step-ca reads its material at process start only. A metadata-only change restarts
   nothing, and at bring-up, before Argo first syncs StepCaDeploy, there is no StatefulSet yet.

The restart decision rests on the data the run read before writing. A run that stops between its
Secret writes and the restart leaves step-ca on its old material, and a re-run finds nothing to
change; restart it by hand with `kubectl -n step-ca-prd rollout restart statefulset step-ca`.

Every task that touches Secret data is `no_log`: `ansible.cfg` makes every run a `--diff` run, and
the module's diff and result carry the data. A failure in those tasks prints only "censored". Task
names carry the Secret's name instead, so `--check` names each Secret a run would change, without
its data.
