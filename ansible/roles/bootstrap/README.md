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

Rotating the `ansible` key means:

1. Generate a new keypair; store private in Roboform + cloud folder.
2. Replace `files/ansible.pub` with the new public key.
3. Run `site.yml` against the full inventory to push the new key.
4. Once the new key is confirmed working, flip `authorized_key.exclusive` to `true` for one run to purge the old key, then flip back.

Same procedure for `pvginkel.pub`.
