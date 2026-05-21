# `haproxy` role

Installs HAProxy and renders `/etc/haproxy/haproxy.cfg` from caller-
supplied `frontend` / `backend` sections. Reusable and host-class-
agnostic: callers `include_role` it with per-call vars, the same way
the [`keepalived`](../keepalived/README.md) role wires up a VIP.

## Inputs

Required (the role asserts these):

- `haproxy_frontends` — list of frontend sections. Each item:
  - `name` — section name (`frontend <name>`); must be unique.
  - `bind` — argument to `bind` (e.g. `"0.0.0.0:443"`).
  - `mode` — `tcp` or `http`.
  - `default_backend` — backend name traffic falls through to.
  - `options` — optional list of raw config lines emitted inside the
    section (e.g. `option httpchk GET /v1/sys/health HTTP/1.1\r\nHost:\ secrets`,
    `http-check expect status 200 429`). The role does not parse
    these — pass exactly what HAProxy expects.
- `haproxy_backends` — list of backend sections. Each item:
  - `name` — section name; referenced by a frontend's `default_backend`.
  - `mode` — `tcp` or `http`; must match the frontend feeding it.
  - `options` — optional list of raw config lines emitted inside the
    section, before `server` lines.
  - `servers` — list of `{ name, address, options }` dicts; `address`
    is `<host-or-ip>:<port>`; `options` is appended verbatim after the
    address (e.g. `check check-ssl verify required ca-file
    /etc/ssl/certs/ca-certificates.crt`).

Optional:

- `haproxy_enabled` — start + enable the service (default `true`).
- `haproxy_global_lines` / `haproxy_defaults_lines` — replace the
  `global` and `defaults` block bodies. Defaults log to syslog, set
  the admin stats socket at `/run/haproxy/admin.sock` mode 660, and
  pick conservative 30-second TCP timeouts.

## What it does

1. `apt install haproxy`.
2. Render `/etc/haproxy/haproxy.cfg` from the inputs. The template is
   validated with `haproxy -c -f` before being installed.
3. Enable + start `haproxy.service`; restart it on a config change.

Every step is idempotent — a converged host reports `ok` throughout.

## Example — OpenBao TCP pass-through (card #10)

```yaml
- ansible.builtin.include_role:
    name: haproxy
  vars:
    haproxy_frontends:
      - name: openbao_https
        bind: "0.0.0.0:443"
        mode: tcp
        default_backend: openbao
    haproxy_backends:
      - name: openbao
        mode: tcp
        servers:
          - name: local
            address: "127.0.0.1:8200"
            options: check
```

No TLS termination — `mode tcp` forwards the client's TLS connection
straight through to OpenBao, whose listener cert reaches the client
unmodified.

## Constraints

- **The role owns `haproxy.cfg`.** A second `include_role` against the
  same host overwrites the first. Co-tenanting multiple consumers on
  one HAProxy means a single caller passing combined frontend +
  backend lists.
