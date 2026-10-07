# SSH host CA rotation runbook

Replaces the homelab SSH host CA's key pair and its passphrase, and
re-signs every host certificate with the new key. The SSH host CA was
set up by
[`step-ca-bootstrap.md` §Enabling the SSH host CA](step-ca-bootstrap.md#enabling-the-ssh-host-ca);
this runbook replaces what that one-shot created.

Clients trust the CA through one `@cert-authority` line, and replacing
that line is what retires the old key. A rotation is therefore a
two-line transition:

1. The new line goes in beside the old one, in every place the line
   lives (step 2).
2. step-ca switches to the new key (step 3).
3. Every host certificate is re-signed (steps 4 and 5).
4. The old line goes, five days after step-ca switched, once KubeCoder
   environments' certificates have rolled over (step 6).

A new passphrase on the old key is no rotation: git history keeps the
old encrypted key beside its old passphrase. Every rotation is a new
key and a new passphrase.

The X.509 side of step-ca has procedures of its own:
[§Intermediate rotation](step-ca-bootstrap.md#intermediate-rotation)
and [`step-ca-root-rotation.md`](step-ca-root-rotation.md).

## Where the trust line lives

| Place | What reads it | How the line changes |
|---|---|---|
| `ansible/files/known_hosts.d/homelab` | Ansible (`UserKnownHostsFile` in `ansible/ansible.cfg`), including the playbooks that put a transient known_hosts beside it | A commit. |
| The `iac` image's `/root/.ssh/known_hosts` | The bpg/proxmox provider in the `IaC/*` pipelines and in srviac's `iac` | A push that changes the repo file rebuilds the image (`IaC/IaC Docker Image`, [`iac-agent.md`](iac-agent.md) §Routine: the `iac` image rebuild). srviac's `iac` pulls `:latest` on every invocation. |
| A KubeCoder environment's `~/.ssh/known_hosts` | The bpg provider and `ssh`, in the environment and its tool sidecars | `scripts/kubecoder-keys.sh`, the first step of `kc project setup`. It adds every `@cert-authority` line of the repo file, and removes a homelab line the repo file no longer carries. |
| The operator's workstation's `~/.ssh/known_hosts` ([`operator-workstation.md`](operator-workstation.md)) | The bpg provider, and `ssh` to hosts and to KubeCoder environments | By hand, with the same script run from the workstation's checkout. With none of its key variables set it writes no key, only the known_hosts lines. |

A homelab line is an `@cert-authority` line whose key comment is
`homelab-ssh-host-ca`. The script finds the lines to remove by that
comment, so every key is generated with it (step 1). Any other machine
you gave the line by hand takes the same two edits by hand.

## Before you start

- step-ca's material is the `step_ca` role's
  ([`step-ca-bootstrap.md`](step-ca-bootstrap.md) §Conventions), and
  StepCaDeploy's chart renders none of step-ca's Secrets. While it
  still does, an Argo sync of the chart puts the old key back.
- You work on `wrkdev`, in `~/source/Ansible` on an up-to-date `main`,
  with `ANSIBLE_VAULT_PASSWORD_FILE` exported
  ([`operator-workstation.md`](operator-workstation.md) §ansible-vault
  passphrase).
- Keep one shell open from step 1 through step 3: `$d` names the
  scratch directory that holds the new key.

### Roboform entries you will replace

| Entry name | What it holds after the rotation |
|---|---|
| `homelab-ca SSH host CA key (encrypted)` | The new encrypted SSH host CA private key. |
| `homelab-ca SSH host CA key passphrase` | The new passphrase decrypting it. |

## 1. Generate the new key pair and passphrase

On `wrkdev`, in a fresh directory:

```sh
d=$(mktemp -d) && cd "$d"
ssh-keygen -t ed25519 -f ssh_host_ca -C homelab-ssh-host-ca
```

At the prompt, enter a new 32+ char passphrase generated in Roboform,
and save it over `homelab-ca SSH host CA key passphrase`. Keep the
comment `homelab-ssh-host-ca` exactly as written: it is how
`scripts/kubecoder-keys.sh` recognises this key's line when this key
is retired in turn. The command writes `ssh_host_ca` (the encrypted
private key) and `ssh_host_ca.pub`.

Copy `ssh_host_ca` into Roboform over
`homelab-ca SSH host CA key (encrypted)`. Then round-trip both
entries: paste the Roboform copy back, and decrypt it with the
passphrase from Roboform when `ssh-keygen -y` asks for it:

```sh
(umask 077 && cat > rt)    # paste the Roboform copy, Ctrl-D
ssh-keygen -y -f rt | cut -d' ' -f1,2 | diff -q - <(cut -d' ' -f1,2 ssh_host_ca.pub) && echo OK
shred -u rt
```

Go on only after `OK`.

## 2. Trust the new key beside the old one

Add the new line to the repo file and push it:

```sh
cd ~/source/Ansible
printf '@cert-authority * %s\n' "$(cat "$d/ssh_host_ca.pub")" >> ansible/files/known_hosts.d/homelab
grep -c '^@cert-authority ' ansible/files/known_hosts.d/homelab    # 2
git add ansible/files/known_hosts.d/homelab
git commit -m 'known_hosts: trust the new SSH host CA beside the old one'
git push
```

Then bring the line to the other three places:

- **The `iac` image.** The push starts `IaC/IaC Docker Image`, which
  rebuilds the image because the push changed the repo file. Wait for
  it to go green. Restart a failed build with `image=iac`
  ([`iac-agent.md`](iac-agent.md) §Routine: the `iac` image rebuild).
- **Every KubeCoder environment with an Ansible checkout.** In each,
  `git pull`, then `./scripts/kubecoder-keys.sh`. It reports
  `added the homelab host CA` for the new line.
- **The workstation.** `./scripts/kubecoder-keys.sh` from
  `~/source/Ansible`.

Check that each place holds both lines:

```sh
# In each KubeCoder environment and on the workstation:
grep -c 'homelab-ssh-host-ca$' ~/.ssh/known_hosts    # 2
# The image:
docker run --rm --pull=always registry:5000/iac:latest grep -c 'homelab-ssh-host-ca$' /root/.ssh/known_hosts    # 2
```

No host has changed yet. Every host still serves a certificate the old
key signed, and every place trusts both keys. Every place must carry
the new line before step 3. From step 3 on, step-ca signs with the new
key: the KubeCoder controller's renewals, the weekly
`IaC/Scheduled Certs` and step 4 then hand out certificates that only
the new line vouches for.

## 3. Switch step-ca to the new key

Put the new key pair and its passphrase into the role's three SSH host
CA files (the table in
[`step-ca-bootstrap.md`](step-ca-bootstrap.md) day-zero step 7), and
check what the playbook would change:

```sh
cd ~/source/Ansible/ansible
cp "$d/ssh_host_ca.pub" roles/step_ca/files/ssh_host_ca_key.pub
poetry run ansible-vault encrypt --output roles/step_ca/files/ssh_host_ca_key "$d/ssh_host_ca"
read -rs pw    # paste the new `homelab-ca SSH host CA key passphrase` from Roboform, then Enter
printf '%s' "$pw" | poetry run ansible-vault encrypt --output roles/step_ca/files/ssh_host_ca_password -
unset pw
poetry run ansible-playbook playbooks/step-ca.yml --check
```

The check reports `changed` on the tasks `Secret step-ca-certs`,
`Secret step-ca-secrets`, `Secret step-ca-ssh-host-ca-password` and
`Restart step-ca on its changed material`. If it also names
`step-ca-config` or `step-ca-ca-password`, the live material differs
from the role's for a reason other than this rotation. Stop and find
out why before you apply.

Apply:

```sh
poetry run ansible-playbook playbooks/step-ca.yml
```

The run writes the three Secrets and restarts StatefulSet `step-ca`,
because their data changed. Confirm that step-ca now publishes the new
key, and only that key:

```sh
curl -s --cacert roles/baseline/files/homelab-root.crt https://ca.home/ssh/roots; echo
cut -d' ' -f2 roles/step_ca/files/ssh_host_ca_key.pub
```

`hostKey` holds one key: the second command's output. If it still
holds the old key, step-ca is running on the material it had before:
a run that stopped between writing the Secrets and restarting step-ca
leaves the next run nothing to restart. Restart it by hand with
`kubectl -n step-ca-prd rollout restart statefulset/step-ca`, then
check again.

Commit the role's files, then shred the scratch directory. The key now
lives in Roboform and, vaulted, in the role.

```sh
git add roles/step_ca/files/ssh_host_ca_key roles/step_ca/files/ssh_host_ca_key.pub roles/step_ca/files/ssh_host_ca_password
git commit -m 'step_ca: switch the SSH host CA to the new key'
git push
shred -u "$d"/* && rmdir "$d"
```

Until step 6, both lines are trusted. Reverting this commit and
running the playbook returns step-ca to the old key without breaking a
host.

## 4. Re-sign every host certificate

```sh
poetry run ansible-playbook playbooks/renew-host-certs.yml -e ssh_host_cert_renewal_threshold_days=48
```

`ssh_host_cert` re-signs a certificate that has less validity left than
the threshold (`roles/ssh_host_cert/tasks/issue.yml`). Certificates
last 47 days, so at 48 every certificate is inside the threshold. The
role re-signs each one and reloads sshd, with no code change.

The playbook reaches `managed:!ceph_prd`: pve, pve1, pve2, srvk8s1–4,
srvk8sdev, wrkdev, srviac and srvvault1–3. It does not reach:

- **srvceph1–3** (`ceph_prd`). No site playbook converges them, so
  they carry no host certificate and have nothing to re-sign.
- **srvk8sdev while it is off**, which it is by default. The run
  reports it UNREACHABLE and re-signs the others. Re-sign it when it
  is next on, with the same command and `--limit srvk8sdev`. Once
  step 6 has removed the old line, that run cannot connect. Use
  `playbooks/reissue-host-cert.yml -e reissue_target=srvk8sdev`
  instead, which connects through the host's pinned key
  ([`ssh-host-cert-expiry.md`](ssh-host-cert-expiry.md)).
- **KubeCoder environments.** The KubeCoder controller signs their
  certificates through step-ca, and Ansible never touches them.
  Step 5 covers them.

## 5. Verify

Every host the run reached serves a certificate the new key signed:

```sh
new=$(ssh-keygen -lf roles/step_ca/files/ssh_host_ca_key.pub | cut -d' ' -f2)
for h in $(poetry run ansible 'managed:!ceph_prd' --list-hosts | tail -n +2); do
  ca=$(ssh-keyscan -c -t ed25519 "$h.home" 2>/dev/null | grep -v '^#' | ssh-keygen -L -f - 2>/dev/null | awk '/Signing CA:/ {print $4}')
  if [ "$ca" = "$new" ]; then echo "new  $h"; else echo "OLD  $h ${ca:-no answer}"; fi
done
```

`ssh-keyscan -c` fetches the host's certificate without logging in,
and `Signing CA` is the fingerprint of the key that signed it. Every
host that is up prints `new`.

A KubeCoder environment's certificate lasts 168 hours, and the
controller re-signs it when under 48 hours remain (KubeCoder
`controller/docs/state/ssh-host-identity.md`). So by five days after
step 3, every running environment serves a certificate the new key
signed. An environment that stayed stopped through those five days
still holds an old certificate. By then that certificate has under
48 hours left, so the controller re-signs it once the environment runs
again. Check a running environment the same way, by its DNS name
`<env-id>.home`:

```sh
ssh-keyscan -c -t ed25519 <env-id>.home 2>/dev/null | grep -v '^#' | ssh-keygen -L -f - | grep -E 'Signing CA|Valid'
```

## 6. Remove the old line, five days after step 3

Do not do this earlier. Until then, a running KubeCoder environment can
still serve a certificate that only the old line vouches for.

```sh
cd ~/source/Ansible
git pull
f=ansible/files/known_hosts.d/homelab
new=$(cut -d' ' -f2 ansible/roles/step_ca/files/ssh_host_ca_key.pub)
awk -v key="$new" '!/^@cert-authority / || index($0, " " key " ")' "$f" >"$f.new" && mv "$f.new" "$f"
git diff    # one line removed: the @cert-authority line without the new key
git add "$f"
git commit -m 'known_hosts: retire the old SSH host CA'
git push
```

The `awk` keeps every line except an `@cert-authority` line that does
not carry the new key.

Then bring the removal to the same three places as in step 2. This
time the script reports `removed a retired homelab host CA`:

- **The `iac` image.** The push rebuilds it. Wait for
  `IaC/IaC Docker Image` to go green.
- **Every KubeCoder environment with an Ansible checkout.**
  `git pull`, then `./scripts/kubecoder-keys.sh`.
- **The workstation.** `./scripts/kubecoder-keys.sh` from
  `~/source/Ansible`.

Check that no place carries the old key:

```sh
grep -c 'homelab-ssh-host-ca$' ~/source/Ansible/ansible/files/known_hosts.d/homelab    # 1
# In each KubeCoder environment and on the workstation:
grep -c 'homelab-ssh-host-ca$' ~/.ssh/known_hosts    # 1
# The image:
docker run --rm --pull=always registry:5000/iac:latest grep -c 'homelab-ssh-host-ca$' /root/.ssh/known_hosts    # 1
```

Then show that the fleet is reachable through the new line alone:

```sh
cd ~/source/Ansible/ansible
poetry run ansible-playbook playbooks/renew-host-certs.yml
```

Every host that is up connects. Step 4's certificates still have more
than the default 14 days left, so the run reports `changed=0`. Last,
connect to a running KubeCoder environment from the workstation: ssh
raises no host-key prompt or warning.
