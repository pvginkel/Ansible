#!/usr/bin/env python3
"""Destroy every AppRole secret_id accessor that no consumer holds.

Run after `. scripts/bao-login.sh` (BAO_ADDR, BAO_TOKEN; BAO_CACERT is honoured):

    scripts/rotation/accessor_cleanup.py [--role ROLE]... [--apply [--jenkins-fallback]]

Per AppRole it reads the secret_id each consumer holds, looks up its accessor
(auth/approle/role/<role>/secret-id/lookup) and plans to destroy every other accessor of the
role (auth/approle/role/<role>/secret-id-accessor/destroy). The plan for every role is printed
before anything is destroyed; without --apply nothing is. A role is left untouched when any of
its consumers cannot be read, or holds a secret_id the lookup does not find.

    openbao-admin  the ansible-vaulted openbao_admin_secret_id
    iac-agent      the OPENBAO_SECRET_ID literal in srviac:/etc/iac/secrets.yaml
    jenkins        every Vault AppRole credential in Jenkins whose role_id is the role's, read
                   through the script console as JENKINS_USER with the API token JENKINS_TOKEN
                   (an administrator) at JENKINS_URL
    eso, eso-dev   every ESO (Cluster)SecretStore on the prd / dev cluster whose role_id is the
                   role's, through ~/.kube/config-<cluster>-write
    backup         /etc/openbao/backup-secret-id on every host of the inventory group openbao

--jenkins-fallback: when the script console read fails, mint one fresh secret_id for jenkins and
show it once on the terminal, for the operator to paste into the Jenkins credential. Its accessor
becomes the held one, and the other jenkins accessors are destroyed only once the operator has
confirmed that a withVault build passed with it.

No secret_id reaches stdout, stderr, a file or a command line; the fallback's fresh secret_id is
the one exception, written to the terminal (/dev/tty) only. Exit status: 0 when every selected
role was proven (and, with --apply, cleaned), 1 when a role was left untouched or a destroy
failed, 2 on a usage error.
"""

import argparse
import base64
import binascii
import functools
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

ROLES = ("openbao-admin", "iac-agent", "jenkins", "eso", "eso-dev", "backup")
REPO = Path(__file__).resolve().parents[2]

# ad-hoc ansible prints its result as one JSON document on stdout; the profile_tasks and timer
# callbacks ansible.cfg enables would print around it.
ANSIBLE = [
    "cexec", "iac", "env",
    "ANSIBLE_LOAD_CALLBACK_PLUGINS=1",
    "ANSIBLE_STDOUT_CALLBACK=ansible.posix.json",
    "ANSIBLE_CALLBACKS_ENABLED=ansible.posix.json",
    "poetry", "run", "ansible",
]
BACKUP_SECRET_ID_FILE = "/etc/openbao/backup-secret-id"
IAC_SECRETS_FILE = "/etc/iac/secrets.yaml"

JENKINS_MARKER = "accessor-cleanup:"
JENKINS_GROOVY = """\
import com.cloudbees.hudson.plugins.folder.AbstractFolder
import com.cloudbees.plugins.credentials.CredentialsProvider
import com.datapipe.jenkins.vault.credentials.VaultAppRoleCredential
import groovy.json.JsonOutput
import jenkins.model.Jenkins

// A folder's stores include its ancestors' stores, so collect each store once.
def stores = new LinkedHashSet()
([Jenkins.get()] + Jenkins.get().getAllItems(AbstractFolder)).each { context ->
  stores.addAll(CredentialsProvider.lookupStores(context).toList())
}
def found = []
stores.each { store ->
  store.domains.each { domain ->
    store.getCredentials(domain).findAll { it instanceof VaultAppRoleCredential }.each { c ->
      found << [store: store.contextDisplayName, id: c.id, roleId: c.roleId,
                secretId: c.secretId.plainText]
    }
  }
}
println('%s' + JsonOutput.toJson(found))
""" % JENKINS_MARKER


class Unproven(Exception):
    """A role's held accessors could not be proven; the message names why, never a value."""


class BaoError(Exception):
    pass


@dataclass
class Holding:
    label: str
    secret_id: str


@dataclass
class Completed:
    returncode: int
    stdout: str
    stderr: str


def run_command(argv: list[str], cwd: Path | None = None) -> Completed:
    try:
        p = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return Completed(124, "", "timed out after 300s")
    return Completed(p.returncode, p.stdout, p.stderr)


def stderr_tail(p: Completed) -> str:
    lines = p.stderr.strip().splitlines()
    return lines[-1] if lines else "no message"


class Bao:
    def __init__(self, addr: str, token: str, cafile: str | None):
        self.addr = addr.rstrip("/")
        self.token = token
        self.open = functools.partial(urllib.request.urlopen,
                                      context=ssl.create_default_context(cafile=cafile),
                                      timeout=30)

    def call(self, method: str, path: str, body: dict | None = None,
             echo_errors: bool = True) -> tuple[int, dict | None]:
        req = urllib.request.Request(
            f"{self.addr}/v1/{path}", method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"X-Vault-Token": self.token, "Content-Type": "application/json"})
        try:
            with self.open(req) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except urllib.error.URLError as e:
            raise BaoError(f"{method} {path}: {e.reason}") from None
        try:
            doc = json.loads(raw) if raw else None
        except ValueError:
            raise BaoError(f"{method} {path}: HTTP {status}, not a JSON answer") from None
        if status >= 400 and status != 404:
            errors = "; ".join((doc or {}).get("errors", [])) if echo_errors else ""
            raise BaoError(f"{method} {path}: HTTP {status}" + (f": {errors}" if errors else ""))
        return status, doc

    def role_id(self, role: str) -> str:
        status, doc = self.call("GET", f"auth/approle/role/{role}/role-id")
        if status == 404:
            raise BaoError(f"no AppRole {role}")
        return doc["data"]["role_id"]

    def accessors(self, role: str) -> list[str]:
        status, doc = self.call("LIST", f"auth/approle/role/{role}/secret-id")
        return [] if status == 404 else doc["data"]["keys"]

    def created(self, role: str, accessor: str) -> str:
        _, doc = self.call("POST", f"auth/approle/role/{role}/secret-id-accessor/lookup",
                           {"secret_id_accessor": accessor})
        return doc["data"]["creation_time"]

    def accessor_of(self, role: str, secret_id: str) -> str | None:
        """The accessor of a live secret_id of the role, None when the role has no such one."""
        status, doc = self.call("POST", f"auth/approle/role/{role}/secret-id/lookup",
                                {"secret_id": secret_id}, echo_errors=False)
        if status in (204, 404) or doc is None:
            return None
        return (doc.get("data") or {}).get("secret_id_accessor")

    def destroy(self, role: str, accessor: str) -> None:
        self.call("POST", f"auth/approle/role/{role}/secret-id-accessor/destroy",
                  {"secret_id_accessor": accessor})

    def mint(self, role: str) -> tuple[str, str]:
        _, doc = self.call("POST", f"auth/approle/role/{role}/secret-id", {})
        return doc["data"]["secret_id"], doc["data"]["secret_id_accessor"]


class JenkinsConsole:
    def __init__(self, url: str, user: str, token: str):
        self.url = url.rstrip("/")
        self.auth = base64.b64encode(f"{user}:{token}".encode()).decode()
        self.open = functools.partial(urllib.request.urlopen, timeout=60)

    def run(self, groovy: str) -> str:
        req = urllib.request.Request(
            f"{self.url}/scriptText", method="POST",
            data=urllib.parse.urlencode({"script": groovy}).encode(),
            headers={"Authorization": f"Basic {self.auth}"})
        try:
            with self.open(req) as resp:
                return resp.read().decode()
        except urllib.error.HTTPError as e:
            raise Unproven(f"the Jenkins script console answered HTTP {e.code}") from None
        except urllib.error.URLError as e:
            raise Unproven(f"the Jenkins script console is unreachable: {e.reason}") from None


class Terminal:
    """The operator's terminal, apart from stdout and stderr, which may be captured."""

    def __init__(self):
        self.tty = open("/dev/tty", "r+")

    def ask(self, prompt: str) -> str:
        self.tty.write(prompt)
        self.tty.flush()
        return self.tty.readline().strip()

    def reveal(self, secret: str, prompt: str) -> None:
        """Show a secret on its own line until the operator presses Enter, then erase it."""
        self.tty.write(f"\n  {secret}\n{prompt}")
        self.tty.flush()
        self.tty.readline()
        # Up three lines (blank, secret, prompt) and clear to the end of the screen.
        self.tty.write("\x1b[3F\x1b[J")
        self.tty.flush()


@dataclass
class Context:
    bao: Bao
    run: Callable[..., Completed] = run_command
    jenkins: JenkinsConsole | None = None
    terminal: Terminal | None = None
    jenkins_fallback: bool = False
    out: Callable[[str], None] = functools.partial(print, flush=True)
    repo: Path = REPO
    home: Path = field(default_factory=Path.home)


def ansible(ctx: Context, pattern: str, module: str, args: str) -> dict[str, dict]:
    """Every targeted host's result; Unproven unless every host succeeded."""
    what = f"ansible {pattern} -m {module} -a {args!r}"
    p = ctx.run([*ANSIBLE, pattern, "-m", module, "-a", args], cwd=ctx.repo / "ansible")
    try:
        doc = json.loads(p.stdout)
        stats = doc["stats"]
        results = doc["plays"][0]["tasks"][0]["hosts"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise Unproven(f"{what}: exit {p.returncode}, no result ({stderr_tail(p)})") from None
    if not stats:
        raise Unproven(f"{what}: no host matched")
    bad = []
    for host in sorted(stats):
        r = results.get(host)
        if r is None:
            bad.append(f"{host}: no result")
        elif r.get("unreachable"):
            bad.append(f"{host}: unreachable: {r.get('msg', '')}")
        elif r.get("failed"):
            bad.append(f"{host}: failed: {r.get('msg', '')}")
    if bad:
        raise Unproven(f"{what}: " + "; ".join(bad))
    if p.returncode != 0:
        raise Unproven(f"{what}: exit {p.returncode} ({stderr_tail(p)})")
    return {host: results[host] for host in stats}


def slurped(host: str, path: str, result: dict) -> str:
    try:
        return base64.b64decode(result["content"], validate=True).decode()
    except (KeyError, binascii.Error, UnicodeDecodeError):
        raise Unproven(f"{host}:{path}: the slurped content does not decode") from None


class _BaoRef:
    """A `!bao mount/path#key` reference: resolved at the iac container's start, not a literal."""


class _SecretsLoader(yaml.SafeLoader):
    pass


_SecretsLoader.add_constructor("!bao", lambda loader, node: _BaoRef())


def read_openbao_admin(ctx: Context, role: str) -> list[Holding]:
    r = ansible(ctx, "srvvault1", "debug", "msg={{ openbao_admin_secret_id }}")["srvvault1"]
    return [Holding("ansible-vault openbao_admin_secret_id", r.get("msg", ""))]


def read_iac_agent(ctx: Context, role: str) -> list[Holding]:
    r = ansible(ctx, "srviac", "slurp", f"src={IAC_SECRETS_FILE}")["srviac"]
    where = f"srviac:{IAC_SECRETS_FILE}"
    try:
        doc = yaml.load(slurped("srviac", IAC_SECRETS_FILE, r), Loader=_SecretsLoader)
    except yaml.YAMLError as e:
        # A YAML error's text quotes the offending source line, which may hold a value.
        mark = getattr(e, "problem_mark", None)
        line = f" at line {mark.line + 1}" if mark else ""
        raise Unproven(f"{where} is not valid YAML{line}") from None
    env = doc.get("env") if isinstance(doc, dict) else None
    values = [e.get("value") for e in env or [] if isinstance(e, dict)
              and e.get("name") == "OPENBAO_SECRET_ID"]
    if len(values) != 1 or not isinstance(values[0], str):
        raise Unproven(f"{where} holds no single literal OPENBAO_SECRET_ID under env")
    return [Holding(f"{where} OPENBAO_SECRET_ID", values[0])]


def read_jenkins(ctx: Context, role: str) -> list[Holding]:
    try:
        return jenkins_holdings(ctx, role)
    except Unproven as e:
        if not ctx.jenkins_fallback:
            raise Unproven(f"{e}; --apply --jenkins-fallback mints a fresh one instead") from None
        return jenkins_fallback(ctx, role, str(e))


def jenkins_holdings(ctx: Context, role: str) -> list[Holding]:
    if ctx.jenkins is None:
        raise Unproven("JENKINS_URL, JENKINS_USER and JENKINS_TOKEN are not all set")
    body = ctx.jenkins.run(JENKINS_GROOVY)
    found = None
    for line in body.splitlines():
        if line.startswith(JENKINS_MARKER):
            try:
                found = json.loads(line[len(JENKINS_MARKER):])
            except ValueError:
                pass
    if not isinstance(found, list):
        raise Unproven("the Jenkins script console returned no credential list")
    role_id = ctx.bao.role_id(role)
    holdings = {}
    for c in found:
        if c["roleId"] == role_id:
            label = f"Jenkins credential {c['id']} ({c['store']})"
            holdings[label] = Holding(label, c["secretId"])
    if not holdings:
        raise Unproven(f"none of the {len(found)} Vault AppRole credentials in Jenkins "
                       f"carries the {role} role_id")
    return list(holdings.values())


def jenkins_fallback(ctx: Context, role: str, why: str) -> list[Holding]:
    ctx.out(f"{role}: the credential read failed: {why}")
    if ctx.terminal.ask(f"Mint one fresh secret_id for {role}? Type 'mint' to go on: ") != "mint":
        raise Unproven(f"{why}; the fallback mint was declined")
    secret_id, accessor = ctx.bao.mint(role)
    ctx.out(f"{role}: minted a fresh secret_id, accessor {accessor}; it goes into the Vault "
            f"AppRole credential the Vault plugin's global configuration names")
    ctx.terminal.reveal(secret_id, "Paste it into that credential, then press Enter: ")
    answer = ctx.terminal.ask(
        "Run a build that reads a secret through withVault. Type 'passed' once it passed "
        "with the new secret_id: ")
    if answer != "passed":
        raise Unproven(f"{why}; the withVault build with the fresh secret_id (accessor "
                       f"{accessor}) was not confirmed")
    return [Holding("Jenkins credential (fresh secret_id, withVault build confirmed)",
                    secret_id)]


def read_eso(cluster: str) -> Callable[[Context, str], list[Holding]]:
    def read(ctx: Context, role: str) -> list[Holding]:
        kubectl = ["cexec", "iac", "kubectl", "--request-timeout=20s",
                   "--kubeconfig", str(ctx.home / ".kube" / f"config-{cluster}-write")]

        def get(*args: str) -> dict:
            p = ctx.run([*kubectl, "get", *args, "-o", "json"])
            if p.returncode != 0:
                raise Unproven(f"{cluster} cluster: kubectl get {' '.join(args)}: "
                               f"exit {p.returncode} ({stderr_tail(p)})")
            return json.loads(p.stdout)

        def secret(ref: dict, namespace: str | None) -> str:
            ns = ref.get("namespace") or namespace
            doc = get("secret", "--namespace", ns, ref["name"])
            try:
                return base64.b64decode(doc["data"][ref["key"]]).decode()
            except (KeyError, binascii.Error, UnicodeDecodeError):
                raise Unproven(f"{cluster} cluster: Secret {ns}/{ref['name']} has no "
                               f"readable key {ref['key']}") from None

        stores = get("clustersecretstores.external-secrets.io,secretstores.external-secrets.io",
                     "--all-namespaces")
        role_id = ctx.bao.role_id(role)
        holdings = []
        for s in stores["items"]:
            approle = (s["spec"].get("provider", {}).get("vault", {})
                       .get("auth", {}).get("appRole"))
            if not approle:
                continue
            ns = s["metadata"].get("namespace")
            store_role_id = approle.get("roleId") or secret(approle["roleRef"], ns)
            if store_role_id != role_id:
                continue
            ref = approle["secretRef"]
            label = (f"{cluster} {s['kind']} {s['metadata']['name']} "
                     f"(Secret {ref.get('namespace') or ns}/{ref['name']}#{ref['key']})")
            holdings.append(Holding(label, secret(ref, ns)))
        if not holdings:
            raise Unproven(f"no ESO store on the {cluster} cluster authenticates as {role}")
        return holdings
    return read


def read_backup(ctx: Context, role: str) -> list[Holding]:
    results = ansible(ctx, "openbao", "slurp", f"src={BACKUP_SECRET_ID_FILE}")
    return [Holding(f"{host}:{BACKUP_SECRET_ID_FILE}",
                    slurped(host, BACKUP_SECRET_ID_FILE, r).strip())
            for host, r in sorted(results.items())]


READERS: dict[str, Callable[[Context, str], list[Holding]]] = {
    "openbao-admin": read_openbao_admin,
    "iac-agent": read_iac_agent,
    "jenkins": read_jenkins,
    "eso": read_eso("prd"),
    "eso-dev": read_eso("dev"),
    "backup": read_backup,
}


@dataclass
class Plan:
    role: str
    untouched: str | None = None
    held: dict[str, list[str]] = field(default_factory=dict)
    destroy: list[str] = field(default_factory=list)
    created: dict[str, str] = field(default_factory=dict)


def plan_role(ctx: Context, role: str) -> Plan:
    try:
        held: dict[str, list[str]] = {}
        for h in READERS[role](ctx, role):
            if not h.secret_id:
                raise Unproven(f"{h.label} is empty")
            accessor = ctx.bao.accessor_of(role, h.secret_id)
            if accessor is None:
                raise Unproven(f"{h.label} holds no live secret_id of {role}")
            held.setdefault(accessor, []).append(h.label)
        live = ctx.bao.accessors(role)
        created = {a: ctx.bao.created(role, a) for a in live}
    except (Unproven, BaoError) as e:
        return Plan(role, untouched=str(e))
    return Plan(role, held=held, destroy=[a for a in live if a not in held], created=created)


def report(ctx: Context, plans: list[Plan], apply: bool) -> None:
    for p in plans:
        if p.untouched:
            ctx.out(f"{p.role}: untouched: {p.untouched}")
            continue
        ctx.out(f"{p.role}: proven")
        for accessor, labels in p.held.items():
            ctx.out(f"  keep     {accessor}  created {p.created.get(accessor, '?')}  "
                    f"held by {', '.join(labels)}")
        for accessor in p.destroy:
            ctx.out(f"  destroy  {accessor}  created {p.created[accessor]}")
    count = sum(len(p.destroy) for p in plans)
    untouched = [p.role for p in plans if p.untouched]
    verb = "destroying" if apply else "would destroy (dry run; --apply destroys)"
    ctx.out(f"{verb} {count} accessor(s); untouched: {', '.join(untouched) or 'none'}")


def execute(ctx: Context, roles: list[str], apply: bool) -> int:
    plans = [plan_role(ctx, role) for role in roles]
    report(ctx, plans, apply)
    if apply:
        for p in plans:
            for accessor in p.destroy:
                try:
                    ctx.bao.destroy(p.role, accessor)
                except BaoError as e:
                    ctx.out(f"stopped: {e}; the accessors reported destroyed above are gone")
                    return 1
                ctx.out(f"destroyed {p.role} {accessor}")
    return 1 if any(p.untouched for p in plans) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--role", action="append", choices=ROLES,
                        help="limit to this AppRole (repeatable; default: all six)")
    parser.add_argument("--apply", action="store_true", help="destroy what the plan names")
    parser.add_argument("--jenkins-fallback", action="store_true",
                        help="mint a fresh jenkins secret_id when the credential read fails")
    args = parser.parse_args(argv)
    roles = [r for r in ROLES if r in (args.role or ROLES)]
    if args.jenkins_fallback and not (args.apply and "jenkins" in roles):
        parser.error("--jenkins-fallback needs --apply and the jenkins role")
    if not os.environ.get("BAO_TOKEN"):
        parser.error("BAO_TOKEN is not set: run `. scripts/bao-login.sh` first")

    jenkins = None
    if all(os.environ.get(v) for v in ("JENKINS_URL", "JENKINS_USER", "JENKINS_TOKEN")):
        jenkins = JenkinsConsole(os.environ["JENKINS_URL"], os.environ["JENKINS_USER"],
                                 os.environ["JENKINS_TOKEN"])
    ctx = Context(
        bao=Bao(os.environ.get("BAO_ADDR", "https://secrets"), os.environ["BAO_TOKEN"],
                os.environ.get("BAO_CACERT")),
        jenkins=jenkins,
        terminal=Terminal() if args.jenkins_fallback else None,
        jenkins_fallback=args.jenkins_fallback,
    )
    return execute(ctx, roles, args.apply)


if __name__ == "__main__":
    sys.exit(main())
