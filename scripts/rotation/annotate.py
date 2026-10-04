#!/usr/bin/env python3
"""Write the rotation annotations of a seed onto the kv mount, and check the mount's contract.

Run after `. scripts/bao-login.sh` (BAO_ADDR, BAO_TOKEN; BAO_CACERT is honoured):

    scripts/rotation/annotate.py [--seed FILE] [--apply]
    scripts/rotation/annotate.py --check
    scripts/rotation/annotate.py --check --keys FILE [--seed FILE]

The apply is a dry run by default: it lists, per leaf, the metadata keys it would add or change.
With --apply it writes them with PATCH kv/metadata/<leaf> (what `bao kv metadata patch` sends),
so keys the seed does not name survive; it never writes data. A seed leaf the store lacks is
reported and skipped, and so is a live leaf the seed does not cover. A write OpenBao refuses
stops the run there. The apply reads metadata only.

The check walks the whole kv mount, reads every leaf's metadata and data key names, and holds
each leaf to design §4's contract (AnsibleSpecs secret-rotation/design.md). With --keys it runs
offline instead: the store is FILE, a JSON object mapping each leaf path to its data key names,
annotated by the seed alone. It prints one line per finding, naming the leaf and the key.

No data value reaches the output: the check keeps only key names. Exit status: 0 when the
apply finished or the check found nothing, 1 otherwise, 2 on a usage error.
"""

import argparse
import datetime
import functools
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

MOUNT = "kv"
DEFAULT_SEED = Path(__file__).resolve().with_name("seed.yaml")

# KV v2 custom_metadata limits (Vault's; not verified for OpenBao 2.5.4).
MAX_KEYS = 64
MAX_KEY_BYTES = 128
MAX_VALUE_BYTES = 512

# Which data keys each kind of design §5 owns (design §3.1; the slice 044 annotation-tool page,
# § Owned keys). ALL: every key. ONE: the leaf's single key that no key_<name> names; with
# several such keys the kind owns none of them. A set: those key names. Keys in IMPLICIT_NONE
# resolve to `none` under the kind without an override.
ALL, ONE = "all", "one"
OWNS: dict[str, str | frozenset[str]] = {
    "random": ALL,
    "manual": ALL,
    "keycloak-client": frozenset({"client_secret"}),
    "cnpg-role": frozenset({"password"}),
    "elastic-user": frozenset({"password"}),
    "approle": ONE,
    "jenkins-token": ONE,
    "youtrack-token": ONE,
    "github-webhook-secret": ONE,
    "home-assistant-token": ONE,
    "google-sa-key": ONE,
    "terraform": ONE,
    "mosquitto-user": ONE,
    "samba-user": ONE,
}
IMPLICIT_NONE = {"keycloak-client": frozenset({"client_id"})}

# The operator-owned keys of design §4: the only ones a seed may name.
SEED_KEYS = {"rotation_mechanism", "rotation_interval", "rotation_activate", "rotation_args",
             "rotation_expires_at", "notes"}
SEED_PREFIXES = ("key_", "interval_")

# Where the seed adds notes to a leaf that already has other notes, the earlier text follows.
NOTES_JOIN = " | earlier: "

COPY = re.compile(r"copy:([^#\s]+)#(.+)")
INTERVAL = re.compile(r"[1-9][0-9]*d|never")
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
LEAF_PATH = re.compile(r"[^/\s]+(/[^/\s]+)*")

# Activators of design §3.3: the argument each takes, or None for none.
WORKLOAD = re.compile(r"[a-z0-9-]+/(deployment|statefulset|daemonset)/[a-z0-9.-]+")
ACTIVATORS: dict[str, re.Pattern | None] = {
    "eso": None,
    "k8s-rollout": WORKLOAD,  # the argument is optional: without it the workloads are derived
    "jenkins-credential": re.compile(r"\S+"),
    "jenkins-job": re.compile(r"[^?\s]+(\?[^=&\s]+=[^&\s]*(&[^=&\s]+=[^&\s]*)*)?"),
    "github-webhook": re.compile(r"\S+/[0-9]+"),
    "argocd-sync": re.compile(r"\S+"),
    "manual": re.compile(r".*\S.*"),
}


class BaoError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class SeedError(Exception):
    pass


class Bao:
    def __init__(self, addr: str, token: str, cafile: str | None = None,
                 opener: Callable | None = None):
        self.addr = addr.rstrip("/")
        self.token = token
        self.open = opener or functools.partial(
            urllib.request.urlopen, context=ssl.create_default_context(cafile=cafile),
            timeout=30)

    def call(self, method: str, path: str, body: dict | None = None,
             content_type: str = "application/json") -> tuple[int, dict | None]:
        req = urllib.request.Request(
            f"{self.addr}/v1/{urllib.parse.quote(path)}", method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"X-Vault-Token": self.token, "Content-Type": content_type})
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
            raise BaoError(f"{method} {path}: HTTP {status}, not a JSON answer", status) from None
        if status >= 400 and status != 404:
            errors = "; ".join(" ".join(e.split()) for e in (doc or {}).get("errors", []))
            raise BaoError(f"{method} {path}: HTTP {status}" + (f": {errors}" if errors else ""),
                           status)
        return status, doc

    def leaves(self, prefix: str = "") -> list[str]:
        status, doc = self.call("LIST", f"{MOUNT}/metadata/{prefix}")
        found = []
        for name in [] if status == 404 else doc["data"]["keys"]:
            if name.endswith("/"):
                found += self.leaves(prefix + name)
            else:
                found.append(prefix + name)
        return sorted(found)

    def metadata(self, leaf: str) -> dict[str, str] | None:
        """The leaf's custom_metadata; None when the store has no such leaf."""
        status, doc = self.call("GET", f"{MOUNT}/metadata/{leaf}")
        if status == 404:
            return None
        return doc["data"].get("custom_metadata") or {}

    def keys(self, leaf: str) -> set[str] | None:
        """The data key names of the leaf's current version; None when it is deleted."""
        status, doc = self.call("GET", f"{MOUNT}/data/{leaf}")
        data = None if status == 404 else (doc.get("data") or {}).get("data")
        return None if data is None else set(data)

    def patch(self, leaf: str, custom: dict[str, str]) -> None:
        self.call("PATCH", f"{MOUNT}/metadata/{leaf}", {"custom_metadata": custom},
                  content_type="application/merge-patch+json")


# --- the seed -------------------------------------------------------------------------------

class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict:
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)
        if key in seen:
            raise SeedError(f"line {key_node.start_mark.line + 1}: {key} appears twice")
        seen.add(key)
    return loader.construct_mapping(node)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def size(text: str) -> int:
    return len(text.encode())


def load_seed(path: Path) -> dict[str, dict[str, str]]:
    """The seed: leaf path -> the metadata keys to write. SeedError lists every problem."""
    try:
        doc = yaml.load(path.read_text(), Loader=_UniqueKeyLoader)
    except yaml.YAMLError as e:
        raise SeedError(f"{path}: not valid YAML: {e}") from None
    if not isinstance(doc, dict):
        raise SeedError(f"{path}: not a mapping of leaf paths")
    problems = []
    for leaf, meta in doc.items():
        if not isinstance(leaf, str) or not LEAF_PATH.fullmatch(leaf):
            problems.append(f"{leaf!r}: not a leaf path under the {MOUNT} mount")
            continue
        if not isinstance(meta, dict) or not meta:
            problems.append(f"{leaf}: not a mapping of metadata keys")
            continue
        if len(meta) > MAX_KEYS:
            problems.append(f"{leaf}: {len(meta)} metadata keys, more than {MAX_KEYS}")
        for key, value in meta.items():
            if not isinstance(key, str) or not (key in SEED_KEYS or key.startswith(SEED_PREFIXES)):
                problems.append(f"{leaf}: {key}: not an operator key of the contract")
            elif size(key) > MAX_KEY_BYTES:
                problems.append(f"{leaf}: {key}: longer than {MAX_KEY_BYTES} bytes")
            elif not isinstance(value, str):
                problems.append(f"{leaf}: {key}: not a string")
            elif size(value) > MAX_VALUE_BYTES:
                problems.append(f"{leaf}: {key}: longer than {MAX_VALUE_BYTES} bytes")
    if problems:
        raise SeedError("\n".join(problems))
    return doc


# --- the check ------------------------------------------------------------------------------

@dataclass
class Leaf:
    path: str
    keys: set[str] | None  # None: the current version's keys cannot be read
    meta: dict[str, str]


@dataclass(frozen=True)
class Finding:
    leaf: str
    key: str
    message: str

    def __str__(self) -> str:
        return f"{self.leaf}: {self.key}: {self.message}"


def kind_error(value: str) -> str | None:
    if value in OWNS or value == "none" or COPY.fullmatch(value):
        return None
    return f"unknown kind {value!r}: not a kind of design §5, none, or copy:<path>#<key>"


def activate_errors(value: str) -> list[str]:
    if value in ("auto", "none"):
        return []
    errors = []
    rollout_targets = False  # whether the previous item was a k8s-rollout target
    for item in value.split(","):
        if rollout_targets and WORKLOAD.fullmatch(item):
            continue  # k8s-rollout:<ns>/<kind>/<name>,<ns>/<kind>/<name>,…
        name, sep, arg = item.partition(":")
        rollout_targets = False
        if item in ("auto", "none"):
            errors.append(f"{item} stands alone, not in a list")
        elif name not in ACTIVATORS:
            errors.append(f"unknown activator {item!r}")
        elif ACTIVATORS[name] is None:
            if sep:
                errors.append(f"{name} takes no argument")
        elif name == "k8s-rollout" and not sep:
            pass
        elif not ACTIVATORS[name].fullmatch(arg):
            errors.append(f"{item!r} is not {name}'s form")
        else:
            rollout_targets = name == "k8s-rollout"
    return errors


def resolve(leaf: Leaf) -> dict[str, str | None]:
    """Each data key's kind; None for a key no kind resolves. Needs leaf.keys."""
    kind = leaf.meta.get("rotation_mechanism")
    overrides = {k[len("key_"):]: v for k, v in leaf.meta.items() if k.startswith("key_")}
    kinds: dict[str, str | None] = {}
    leftover = []
    for key in sorted(leaf.keys):
        if key in overrides:
            kinds[key] = overrides[key] if kind_error(overrides[key]) is None else None
        elif kind is None or kind_error(kind):
            kinds[key] = None
        elif kind == "none" or COPY.fullmatch(kind) or OWNS[kind] == ALL:
            kinds[key] = kind
        elif OWNS[kind] == ONE:
            leftover.append(key)
        elif key in OWNS[kind]:
            kinds[key] = kind
        elif key in IMPLICIT_NONE.get(kind, ()):
            kinds[key] = "none"
        else:
            kinds[key] = None
    for key in leftover:
        kinds[key] = kind if len(leftover) == 1 else None
    return kinds


def is_copy_or_none(kind: str | None) -> bool:
    return kind is not None and (kind == "none" or COPY.fullmatch(kind) is not None)


def check_leaf(leaf: Leaf, store: dict[str, Leaf]) -> list[Finding]:
    m = leaf.meta
    findings = []

    def find(key: str, message: str) -> None:
        findings.append(Finding(leaf.path, key, message))

    kind = m.get("rotation_mechanism")
    has_notes = bool(m.get("notes", "").strip())
    kinds = None
    if leaf.keys is None:
        find("(data)", "its current version is deleted or destroyed: its keys cannot be read")
    else:
        kinds = resolve(leaf)

    if kind is None:
        find("rotation_mechanism", "missing")
    elif err := kind_error(kind):
        find("rotation_mechanism", err)
    if "rotation_activate" not in m:
        find("rotation_activate", "missing")
    if ("rotation_interval" not in m and kinds is not None
            and not all(is_copy_or_none(k) for k in kinds.values())):
        find("rotation_interval", "missing (a key of the leaf is neither a copy nor none)")

    for meta_key, value in sorted(m.items()):
        if meta_key.startswith("key_"):
            name = meta_key[len("key_"):]
            if err := kind_error(value):
                find(meta_key, err)
            if leaf.keys is not None and name not in leaf.keys:
                find(meta_key, f"stale override: the leaf has no key {name!r}")
        elif meta_key.startswith("interval_"):
            name = meta_key[len("interval_"):]
            if not INTERVAL.fullmatch(value):
                find(meta_key, f"{value!r} is not <n>d or never")
            elif value == "never" and not has_notes:
                find(meta_key, "never without the leaf's notes")
            if leaf.keys is not None and name not in leaf.keys:
                find(meta_key, f"the leaf has no key {name!r}")
            elif kinds is not None and is_copy_or_none(kinds.get(name)):
                find(meta_key, f"key {name!r} is {kinds[name]}, which takes no interval")

    if kinds is not None and kind is not None and kind_error(kind) is None:
        for key, k in kinds.items():
            if k is None and not (f"key_{key}" in m and kind_error(m[f"key_{key}"])):
                find(key, f"no kind resolves it: {kind} does not own it and no key_{key} "
                          f"names one")
    if kinds is not None:
        for key, k in kinds.items():
            copy = COPY.fullmatch(k or "")
            if not copy:
                continue
            primary, primary_key = copy.groups()
            source = f"key_{key}" if f"key_{key}" in m else "rotation_mechanism"
            if primary not in store:
                find(source, f"copy of {primary}#{primary_key}: no leaf {primary}")
            elif store[primary].keys is not None and primary_key not in store[primary].keys:
                find(source, f"copy of {primary}#{primary_key}: {primary} has no key "
                             f"{primary_key!r}")

    interval = m.get("rotation_interval")
    if interval is not None:
        if not INTERVAL.fullmatch(interval):
            find("rotation_interval", f"{interval!r} is not <n>d or never")
        elif interval == "never" and not has_notes:
            find("rotation_interval", "never without notes")
    if "rotation_activate" in m:
        for err in activate_errors(m["rotation_activate"]):
            find("rotation_activate", err)
    if "rotation_args" in m:
        args = m["rotation_args"]
        try:
            json.loads(args)
        except ValueError:
            find("rotation_args", "not JSON")
        if size(args) > MAX_VALUE_BYTES:
            find("rotation_args", f"larger than {MAX_VALUE_BYTES} bytes")
    if "rotation_expires_at" in m:
        expires = m["rotation_expires_at"]
        try:
            valid = bool(ISO_DATE.fullmatch(expires)) and bool(datetime.date.fromisoformat(expires))
        except ValueError:
            valid = False
        if not valid:
            find("rotation_expires_at", f"{expires!r} is not an ISO date (YYYY-MM-DD)")
    return findings


def check(store: dict[str, Leaf]) -> list[Finding]:
    return [f for path in sorted(store) for f in check_leaf(store[path], store)]


# --- the apply ------------------------------------------------------------------------------

def changes(seed: dict[str, str], current: dict[str, str]) -> dict[str, str]:
    """The metadata keys the seed adds or changes; earlier notes are kept after the seed's."""
    out = {}
    for key, value in seed.items():
        have = current.get(key)
        if key == "notes" and have and value != have:
            if have.startswith(value + NOTES_JOIN):
                continue
            value = f"{value}{NOTES_JOIN}{have}"
        if have != value:
            out[key] = value
    return out


@dataclass
class Plan:
    patches: dict[str, dict[str, str]] = field(default_factory=dict)
    current: dict[str, dict[str, str]] = field(default_factory=dict)
    unchanged: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def make_plan(bao: Bao, seed: dict[str, dict[str, str]]) -> Plan:
    plan = Plan()
    live = bao.leaves()
    plan.uncovered = [leaf for leaf in live if leaf not in seed]
    live_set = set(live)
    for leaf in sorted(seed):
        current = bao.metadata(leaf) if leaf in live_set else None
        if current is None:
            plan.absent.append(leaf)
            continue
        patch = changes(seed[leaf], current)
        if not patch:
            plan.unchanged.append(leaf)
            continue
        plan.patches[leaf], plan.current[leaf] = patch, current
        if size(patch.get("notes", "")) > MAX_VALUE_BYTES:
            plan.errors.append(f"{leaf}: notes: with the earlier notes kept, longer than "
                               f"{MAX_VALUE_BYTES} bytes")
        if len(current.keys() | patch.keys()) > MAX_KEYS:
            plan.errors.append(f"{leaf}: more than {MAX_KEYS} metadata keys once patched")
    return plan


def report(out: Callable[[str], None], plan: Plan, apply: bool) -> None:
    for leaf, patch in plan.patches.items():
        out(leaf)
        for key, value in patch.items():
            have = plan.current[leaf].get(key)
            out(f"  add     {key}={value}" if have is None
                else f"  change  {key}={value}  (was {have})")
    for leaf in plan.absent:
        out(f"absent from the store, skipped: {leaf}")
    for leaf in plan.uncovered:
        out(f"not in the seed: {leaf}")
    verb = "patching" if apply else "would patch (dry run; --apply writes)"
    out(f"{verb} {len(plan.patches)} leaf(s); {len(plan.unchanged)} unchanged, "
        f"{len(plan.absent)} absent from the store, {len(plan.uncovered)} live leaf(s) "
        f"not in the seed")


def run_apply(bao: Bao, seed: dict[str, dict[str, str]], apply: bool,
              out: Callable[[str], None]) -> int:
    plan = make_plan(bao, seed)
    report(out, plan, apply)
    if plan.errors:
        for error in plan.errors:
            out(f"cannot write: {error}")
        out("nothing written")
        return 1
    if not apply:
        return 0
    for done, (leaf, patch) in enumerate(plan.patches.items()):
        try:
            bao.patch(leaf, patch)
        except BaoError as e:
            if e.status == 403:
                out(f"stopped at {leaf}: OpenBao refused the write ({e}). The token's policy "
                    f"lacks the patch capability on the {MOUNT} mount; the site-openbao.yml "
                    f"converge grants it to openbao-admin. Patched {done} of "
                    f"{len(plan.patches)} leaf(s); run the apply again after the converge.")
            else:
                out(f"stopped at {leaf}: {e}. Patched {done} of {len(plan.patches)} leaf(s).")
            return 1
        out(f"patched {leaf}")
    return 0


# --- main -----------------------------------------------------------------------------------

def live_store(bao: Bao) -> dict[str, Leaf]:
    return {leaf: Leaf(leaf, bao.keys(leaf), bao.metadata(leaf) or {})
            for leaf in bao.leaves()}


def offline_store(keys_file: Path, seed: dict[str, dict[str, str]],
                  out: Callable[[str], None]) -> dict[str, Leaf]:
    try:
        doc = json.loads(keys_file.read_text())
    except ValueError:
        raise SeedError(f"{keys_file}: not JSON") from None
    if not isinstance(doc, dict) or not all(
            isinstance(v, list) and all(isinstance(k, str) for k in v) for v in doc.values()):
        raise SeedError(f"{keys_file}: not a JSON object of leaf path -> key names")
    for leaf in sorted(set(seed) - set(doc)):
        out(f"seed leaf not in the key file: {leaf}")
    return {leaf: Leaf(leaf, set(keys), dict(seed.get(leaf, {}))) for leaf, keys in doc.items()}


def run_check(store: dict[str, Leaf], out: Callable[[str], None]) -> int:
    findings = check(store)
    for f in findings:
        out(str(f))
    out(f"{len(findings)} finding(s) on {len({f.leaf for f in findings})} of {len(store)} "
        f"leaf(s)")
    return 1 if findings else 0


PRINT = functools.partial(print, flush=True)


def main(argv: list[str] | None = None, opener: Callable | None = None,
         out: Callable[[str], None] = PRINT) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=Path,
                        help=f"the seed (default: {DEFAULT_SEED.name} beside this script)")
    parser.add_argument("--apply", action="store_true", help="write what the dry run lists")
    parser.add_argument("--check", action="store_true", help="check the contract")
    parser.add_argument("--keys", type=Path,
                        help="with --check: check the seed offline over these key names")
    args = parser.parse_args(argv)
    if args.check and args.apply:
        parser.error("--check and --apply exclude each other")
    if args.keys and not args.check:
        parser.error("--keys needs --check")
    if args.check and args.seed and not args.keys:
        parser.error("the live --check reads the store, not a seed")
    seed_path = args.seed or DEFAULT_SEED
    if not args.keys and not os.environ.get("BAO_TOKEN"):
        parser.error("BAO_TOKEN is not set: run `. scripts/bao-login.sh` first")

    try:
        if args.keys:
            return run_check(offline_store(args.keys, load_seed(seed_path), out), out)
        bao = Bao(os.environ.get("BAO_ADDR", "https://secrets"), os.environ["BAO_TOKEN"],
                  os.environ.get("BAO_CACERT"), opener)
        if args.check:
            return run_check(live_store(bao), out)
        return run_apply(bao, load_seed(seed_path), args.apply, out)
    except (SeedError, BaoError, OSError) as e:
        out(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
