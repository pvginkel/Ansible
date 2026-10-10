# `bootstrap` role

Establishes the two baseline user accounts on a managed Ubuntu host.

| User | UID | SSH key | Sudo |
|---|---|---|---|
| `ansible` | 900 | `files/ansible.pub` (ed25519) | `NOPASSWD` via `/etc/sudoers.d/ansible` |
| `pvginkel` | 1000 | `files/pvginkel.pub` (ed25519) | `%sudo` group default — password required |

Idempotent: safe to re-run on a host already in the right state. Cloud-init on scratch VMs creates the `ansible` user up front; on those hosts the `ansible` tasks are no-ops. `pvginkel` is always role-managed.

## First-login caveat for pvginkel

The role does not set a password for `pvginkel`. Ubuntu `useradd` locks the account password by default, so password-based login is disabled but SSH key login works.

`sudo` on Ubuntu's default `/etc/sudoers` line (`%sudo ALL=(ALL:ALL) ALL`) requires a password — so `pvginkel` cannot run `sudo` until a password is set. Once per host, as root or via the `ansible` account:

```sh
sudo passwd pvginkel
```

Set the password to whatever the operator uses for interactive `sudo`. This is a one-time step per host and not automated intentionally — no long-lived secret ends up in the repo or in Ansible.

## SSH key rotation

The `pvginkel` key is rotated by hand:

1. Generate a new keypair; store private in Roboform + cloud folder.
2. Replace `files/pvginkel.pub` with the new public key.
3. Run `site.yml` against the full inventory to push the new key.
4. Once the new key is confirmed working, flip `authorized_key.exclusive` to `true` for one run to purge the old key, then flip back.

How the `ansible` key is rotated depends on whether SecretRotator's `ssh-key` kind is in its nightly run's `kinds_enabled`. Until it is, the `ansible` key is rotated by hand too, by the same procedure on `files/ansible.pub`. srviac and the KubeCoder environments read its private half from OpenBao, so a hand rotation also writes it to `kv/iac/ansible-ssh-key#private` and its catalog copy `eso/prd/kubecoder/prd/catalog#ssh-key-ansible`.

Once the kind is enabled, it rotates the `ansible` key every 14 days with no operator step. It authorises a new key on every host that holds the key (`playbooks/rotate-ansible-key.yml`) and logs in to each with it. It commits the new public half to `files/ansible.pub` on `main`, and writes the private half to OpenBao and to srviac's own copy of the key. Last, it takes the old public half off every host. Do not rotate the `ansible` key by hand then. RoboForm and the cloud folder hold no copy of it, and a person's way in without OpenBao is `pvginkel`.
