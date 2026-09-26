#!/usr/bin/env python3
"""Resource requests for every app-stage Argo CD deploys, from Prometheus (argo-cd D65).

    report DIR   clone every deploy repo in ArgoCDDeploy's registry into DIR/repos, read the last
                 7 days from Prometheus, and write one patch per deploy repo it would change into
                 DIR/report; containers it cannot place are listed in DIR/not-placed.txt
    apply DIR    apply the patches left in DIR/report to their clones and commit each on main

Between the two steps the report is the operator's: deleting a patch leaves that deploy repo as it
is, and an edited hunk is applied as edited. Nothing is pushed; `apply` prints the push commands.

The recommendation policy is HelmCharts' tools/chart_tools/recommend_resources.py's: p75 CPU and
p90 working-set memory over 7 days, CPU rounded up to one significant digit, memory up to the next
quarter power of two, and a value only ever raised unless --reset. A container's requests go in
its app-stage's config/<stage>/values.yaml, at `resources.<workload>.<container>.requests` when
the stage's resolved chart declares that container there, else at the path the chart's map in
resources-entry-maps/<chart>.json names. The resolved chart is the deploy repo's chart/, or for an
upstream app the registry's chart at the stage's pinned version.

Run from the dev container; helm runs in the `iac` sidecar via cexec.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from typing import Any, Optional

import yaml

HERE = Path(__file__).resolve().parent
REGISTRY = Path("/work/ArgoCDDeploy/releases/values.yaml")
MAPS = HERE / "resources-entry-maps"
PROMETHEUS = "http://prometheus.home"
NUM_DAYS = 7
# Changes enter a deploy repo on main; a stage that tracks another branch, like KubeCoder's prd
# (argo-cd D34), receives them by promotion.
ENTRY_BRANCH = "main"
COMMIT_MESSAGE = f"""\
config: resource requests from {NUM_DAYS} days of Prometheus data

p75 CPU and p90 working-set memory, by Ansible's support/recommend-resources (argo-cd D65).
"""


class Stop(Exception):
    pass


def log(msg: str) -> None:
    print(f"[recommend-resources] {msg}", flush=True)


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if check and r.returncode != 0:
        raise Stop(f"command failed ({r.returncode}): {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    return r


def git(clone: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(clone), *args], check=check)


# ---------------------------------------------------------------------------------------------
# The registry and the resolved chart


@dataclass(frozen=True)
class Stage:
    app: str
    stage: str
    repo: str
    upstream: Optional[dict]
    version: Optional[str]
    revision: str

    @property
    def ns(self) -> str:
        return f"{self.app}-{self.stage}"

    @property
    def clone(self) -> str:
        return self.repo.rsplit("/", 1)[1].removesuffix(".git")

    @property
    def values_file(self) -> str:
        return f"config/{self.stage}/values.yaml"


def registry_stages(registry: dict) -> dict[str, Stage]:
    """Namespace -> app-stage; the namespace is always <app>-<stage> (argo-cd D24)."""
    stages = {}
    for app, entry in registry["apps"].items():
        for stage, settings in entry["stages"].items():
            s = Stage(app, stage, entry["repo"], entry.get("upstream"), settings.get("version"),
                      settings.get("targetRevision", "main"))
            stages[s.ns] = s
    return stages


@dataclass
class Chart:
    name: str
    values: dict


@cache
def upstream_values(repo: str, chart: str, version: str) -> dict:
    out = run(["cexec", "iac", "helm", "show", "values", chart, "--repo", repo, "--version", version],
              cwd=HERE).stdout
    return yaml.safe_load(out) or {}


def resolve_chart(stage: Stage, clone: Path) -> Chart:
    if stage.upstream:
        chart = stage.upstream["chart"]
        return Chart(chart, upstream_values(stage.upstream["repo"], chart, stage.version))
    meta = yaml.safe_load((clone / "chart/Chart.yaml").read_text())
    return Chart(meta["name"], yaml.safe_load((clone / "chart/values.yaml").read_text()) or {})


# ---------------------------------------------------------------------------------------------
# The recommendations


@dataclass(frozen=True)
class ContainerRef:
    ns: str
    pod: str
    container: str


@dataclass
class Recommendation:
    cpu: int
    memory: int


# Strips controller-generated pod suffixes to recover the workload name:
#   Deployment / CronJob: <workload>-<8-10 char hash>-<5 char id>
#   DaemonSet / Job:      <workload>-<5 char id>
#   StatefulSet:          <workload>-<ordinal>
_POD_SUFFIX_RE = re.compile(r"-(?:[a-z0-9]{8,10}-[a-z0-9]{5}|[a-z0-9]{5}|\d+)$")


def infer_workload(pod: str) -> Optional[str]:
    m = _POD_SUFFIX_RE.search(pod)
    return pod[: m.start()] if m else None


def promql(query: str, start: int, end: int, step: str = "300s") -> list:
    params = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": step})
    with urllib.request.urlopen(f"{PROMETHEUS}/api/v1/query_range?{params}", timeout=300) as r:
        return json.load(r)["data"]["result"]


def get_values_from_metrics(metrics: Any) -> dict[ContainerRef, list[float]]:
    values = defaultdict(list)

    for result in metrics:
        try:
            ns = result["metric"]["namespace"]
            pod = result["metric"]["pod"]
            container = result["metric"]["container"]

            workload = infer_workload(pod)
            if not workload:
                continue

            key = ContainerRef(ns, workload, container)

            values[key].extend([float(val[1]) for val in result["values"]])
        except KeyError:
            continue

    return values


def percentile(values: list[float], q: float) -> float:
    """numpy.percentile's default (linear) method, which the old tool used."""
    s = sorted(values)
    h = (len(s) - 1) * q / 100
    lo = math.floor(h)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (h - lo) * (s[hi] - s[lo])


def get_recommendations(start: datetime, end: datetime) -> dict[ContainerRef, Recommendation]:
    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())

    cpu_metrics = promql(
        """
        sum by (namespace, pod, container) (
            rate(container_cpu_usage_seconds_total{container != "", container != "POD"}[5m])
        )
        """,
        start_ts,
        end_ts,
    )

    # working_set, not usage: container_memory_usage_bytes includes inactive
    # page cache, which is reclaimable and is not what the scheduler packs
    # against or what kubelet evicts on. Any container that moves bytes through
    # the filesystem reads far higher on usage than it ever needs — registry-prd
    # measured 727 MiB usage against 63 MiB working set.
    mem_metrics = promql(
        """
        max by (namespace, pod, container) (
            container_memory_working_set_bytes{container != "", container != "POD"}
        )
        """,
        start_ts,
        end_ts,
    )

    cpu_data = get_values_from_metrics(cpu_metrics)
    mem_data = get_values_from_metrics(mem_metrics)

    recommendations = {}

    for key in set(cpu_data.keys() | mem_data.keys()):
        cpu = cpu_data.get(key, [])
        mem = mem_data.get(key, [])

        cpu_req = (percentile(cpu, 75) if len(cpu) > 0 else 0) * 1000  # millicores
        mem_req = (percentile(mem, 90) if len(mem) > 0 else 0) / (1024 * 1024)  # MiB

        recommendations[key] = Recommendation(int(round(cpu_req)), int(round(mem_req)))

    return recommendations


def round_cpu_recommendation(value: int) -> int:
    """Round up to 1 significant digit (e.g., 13 -> 20, 371 -> 400)"""
    if value < 10:
        return 0

    power = math.floor(math.log10(abs(value)))
    base = 10**power
    rounded = math.ceil(value / base) * base

    return int(rounded)


def round_mem_recommendation(value: int) -> int:
    """Return the next quarter-power-of-2 >= x (in Mi)"""

    if value <= 0:
        return 0

    exponent = math.ceil(math.log2(value))
    previous_power2 = 2 ** (exponent - 1)
    step = previous_power2 // 4

    for i in range(1, 5):
        match = previous_power2 + step * (i - 1)
        if value <= match:
            return match

    return 2**exponent


def format_cpu(value: int) -> Optional[str]:
    return f"{int(round(value))}m" if value > 0 else None


def format_mem(value: int) -> Optional[str]:
    return f"{int(round(value))}Mi" if value > 0 else None


def parse_cpu(value: Optional[str]) -> int:
    if not value:
        return 0

    if value.endswith("m"):
        return int(value[:-1])
    elif value.endswith("c"):
        return int(value[:-1]) // 1000  # Convert from cores to millicores
    else:
        raise ValueError(f"Invalid CPU value format: {value}")


def parse_mem(value: Optional[str]) -> int:
    if not value:
        return 0

    if value.endswith("Mi"):
        return int(value[:-2])
    elif value.endswith("Gi"):
        return int(value[:-2]) * 1024  # Convert GiB to MiB
    elif value.endswith("Ki"):
        return int(value[:-2]) // 1024  # Convert KiB to MiB
    else:
        raise ValueError(f"Invalid memory value format: {value}")


# ---------------------------------------------------------------------------------------------
# Where a container's requests go


def is_resource_defined(ref: ContainerRef, chart: Chart) -> bool:
    """Whether the chart's values declare resources.<workload>.<container>."""
    resources = chart.values.get("resources")
    if not isinstance(resources, dict) or ref.pod not in resources:
        return False

    containers = resources[ref.pod]
    if not isinstance(containers, dict):
        log(f"chart {chart.name} declares resources.{ref.pod} without a container map")
        return False

    return ref.container in containers


@cache
def entry_map(chart: str) -> dict[str, str]:
    path = MAPS / f"{chart}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def get_resources_path(ref: ContainerRef, chart: Chart) -> Optional[list[str]]:
    if is_resource_defined(ref, chart):
        return ["resources", ref.pod, ref.container, "requests"]

    path = entry_map(chart.name).get(f"{ref.pod}/{ref.container}")
    return path.split(".") if path else None


# ---------------------------------------------------------------------------------------------
# Editing a values file: every line the change does not need stays as it was, comments included.

NULL_TAG = "tag:yaml.org,2002:null"


def _compose(text: str) -> Optional[yaml.Node]:
    return yaml.compose(text, Loader=yaml.SafeLoader)


def _pair(node: yaml.MappingNode, key: str) -> Optional[tuple[yaml.Node, yaml.Node]]:
    for k, v in node.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return k, v
    return None


def _implicit_null(node: yaml.Node) -> bool:
    """`key:` with nothing after it; its marks point at the next token, not at the key."""
    return isinstance(node, yaml.ScalarNode) and node.tag == NULL_TAG and node.value == ""


def _end_line(k: yaml.Node, v: yaml.Node) -> int:
    """The last line of the mapping entry `k: v`."""
    return k.end_mark.line if _implicit_null(v) else _node_end(v)


def _node_end(node: yaml.Node) -> int:
    if isinstance(node, yaml.MappingNode) and not node.flow_style:
        return _end_line(*node.value[-1])
    if isinstance(node, yaml.SequenceNode) and not node.flow_style:
        return _node_end(node.value[-1])
    m = node.end_mark
    return m.line if m.column else m.line - 1


def _insert(text: str, after: int, new: list[str]) -> str:
    lines = text.splitlines(keepends=True)
    if not lines[after].endswith("\n"):
        lines[after] += "\n"
    lines[after + 1:after + 1] = new
    return "".join(lines)


def _append(text: str, new: list[str]) -> str:
    if text and not text.endswith("\n"):
        text += "\n"
    sep = "\n" if text.strip() and not text.endswith("\n\n") else ""
    return text + sep + "".join(new)


def _block(path: list[str], value: str, indent: int) -> list[str]:
    lines = [" " * (indent + 2 * i) + f"{key}:\n" for i, key in enumerate(path[:-1])]
    return [*lines, " " * (indent + 2 * (len(path) - 1)) + f"{path[-1]}: {value}\n"]


def _flow(text: str, node: yaml.MappingNode, edit) -> str:
    """Re-emits a flow mapping after `edit` changes its data; comments inside it are lost."""
    data = yaml.safe_load(text[node.start_mark.index:node.end_mark.index])
    edit(data)
    new = yaml.safe_dump(data, default_flow_style=True, width=math.inf, sort_keys=False).strip()
    return text[:node.start_mark.index] + new + text[node.end_mark.index:]


def _nested_set(data: dict, path: list[str], value: str) -> None:
    for key in path[:-1]:
        if not isinstance(data.get(key), dict):
            data[key] = {}
        data = data[key]
    data[path[-1]] = value


def _nested_pop(data: dict, path: list[str]) -> None:
    for key in path[:-1]:
        data = data.get(key)
        if not isinstance(data, dict):
            return
    data.pop(path[-1], None)


def set_value(text: str, path: list[str], value: str) -> str:
    """`text` with the scalar at `path` set to `value`. Mappings the path lacks are added in block
    style, at the end of the deepest one it has; a new top-level key gets a blank line before it."""
    node = _compose(text)
    if node is None:
        return _append(text, _block(path, value, 0))
    root = node
    for d, key in enumerate(path):
        if not isinstance(node, yaml.MappingNode):
            raise Stop(f"{'.'.join(path[:d]) or 'the document'} is not a mapping")
        if node.flow_style:
            return _flow(text, node, lambda data: _nested_set(data, path[d:], value))
        pair = _pair(node, key)
        if pair is None:
            new = _block(path[d:], value, node.value[0][0].start_mark.column)
            if node is root:
                return _append(text, new)
            return _insert(text, _end_line(*node.value[-1]), new)
        k, v = pair
        if d == len(path) - 1:
            if not isinstance(v, yaml.ScalarNode):
                raise Stop(f"{'.'.join(path)} is not a scalar")
            if _implicit_null(v):
                colon = text.index(":", k.end_mark.index)
                return text[:colon + 1] + f" {value}" + text[colon + 1:]
            quote = v.style if v.style in ("'", '"') else ""
            return text[:v.start_mark.index] + f"{quote}{value}{quote}" + text[v.end_mark.index:]
        if isinstance(v, yaml.MappingNode) and v.value:
            node = v
            continue
        if isinstance(v, yaml.MappingNode) or (isinstance(v, yaml.ScalarNode) and v.tag == NULL_TAG):
            # `key:`, `key: ~` or `key: {}`: the rest of the path becomes the key's block.
            if not _implicit_null(v):
                colon = text.index(":", k.end_mark.index)
                text = text[:colon + 1] + text[v.end_mark.index:]
            return _insert(text, k.start_mark.line, _block(path[d + 1:], value, k.start_mark.column + 2))
        raise Stop(f"{'.'.join(path[:d + 1])} is {v.value!r}, not a mapping")
    raise AssertionError("unreachable")


def delete_value(text: str, path: list[str]) -> str:
    """`text` without the key at `path`; a mapping it empties stays, as `{}`."""
    node, owner = _compose(text), None
    for d, key in enumerate(path):
        if not isinstance(node, yaml.MappingNode):
            return text
        if node.flow_style:
            return _flow(text, node, lambda data: _nested_pop(data, path[d:]))
        pair = _pair(node, key)
        if pair is None:
            return text
        k, v = pair
        if d < len(path) - 1:
            node, owner = v, k
            continue
        lines = text.splitlines(keepends=True)
        del lines[k.start_mark.line:_end_line(k, v) + 1]
        if len(node.value) == 1 and owner is not None:
            # `{}`, not an empty value: null in a values file deletes the chart's default, and a
            # dropped request means falling back to it.
            line = lines[owner.start_mark.line]
            colon = line.index(":", owner.end_mark.column)
            lines[owner.start_mark.line] = line[:colon + 1] + " {}" + line[colon + 1:]
        return "".join(lines)
    raise AssertionError("unreachable")


def _get(data: Any, path: list[str]) -> Any:
    for key in path:
        data = data.get(key) if isinstance(data, dict) else None
    return data


def revise(text: str, path: list[str], rec: Recommendation, reset: bool) -> tuple[str, list[str]]:
    """The values text with one container's requests revised, and what changed."""
    old = _get(yaml.safe_load(text), path)
    old = old if isinstance(old, dict) else {}
    changes = []
    for field, parse, new in (
        ("cpu", parse_cpu, format_cpu(round_cpu_recommendation(rec.cpu))),
        ("memory", parse_mem, format_mem(round_mem_recommendation(rec.memory))),
    ):
        # Recommendations normally only ratchet upwards: a high-water mark is
        # evidence the container really needed that much, and a quiet measurement
        # window is not evidence it stopped. --reset drops that guard so a stale
        # mark can be re-derived from the current window.
        if not (reset or parse(new) > parse(old.get(field))):
            continue
        revised = set_value(text, [*path, field], new) if new else delete_value(text, [*path, field])
        if revised != text:
            changes.append(f"{field} {old.get(field) or 'unset'} -> {new or 'unset'}")
            text = revised
    return text, changes


# ---------------------------------------------------------------------------------------------
# Step one: the report


def clone_all(stages: dict[str, Stage], repos: Path) -> None:
    urls = sorted({(s.clone, s.repo) for s in stages.values()})
    repos.mkdir(parents=True)

    def clone(item: tuple[str, str]) -> None:
        name, url = item
        run(["git", "clone", "--quiet", "--branch", ENTRY_BRANCH, url, str(repos / name)])

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(clone, urls))
    log(f"cloned {len(urls)} deploy repos into {repos}")


def preamble(name: str, start: datetime, end: datetime, lines: list[str]) -> str:
    when = f"{start:%Y-%m-%d %H:%MZ} to {end:%Y-%m-%d %H:%MZ}"
    head = [
        f"recommend-resources: {name}",
        f"Requests from Prometheus, {when}: p75 CPU, p90 working-set memory.",
        f"Delete this file to leave {name} as it is, or edit a hunk to overrule it;",
        f"`recommend_resources.py apply` commits what is left on {ENTRY_BRANCH}. Lines before the",
        "first `diff --git` are ignored.",
        "",
    ]
    return "".join(f"# {line}".rstrip() + "\n" for line in [*head, *lines, ""])


def cmd_report(args: argparse.Namespace) -> None:
    work = Path(args.workdir)
    if work.exists():
        raise Stop(f"{work} exists: step one starts from fresh clones")
    stages = registry_stages(yaml.safe_load(Path(args.registry).read_text()))
    repos, report = work / "repos", work / "report"
    clone_all(stages, repos)
    charts = {ns: resolve_chart(s, repos / s.clone) for ns, s in stages.items()}

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=NUM_DAYS)
    recommendations = get_recommendations(start, end)

    texts: dict[Path, str] = {}
    notes: dict[str, list[str]] = defaultdict(list)
    promoted: dict[str, set[str]] = defaultdict(set)
    unplaced = []
    for ref in sorted(recommendations, key=lambda r: (r.ns, r.pod, r.container)):
        stage = stages.get(ref.ns)
        if not stage:
            continue
        rec = recommendations[ref]
        path = get_resources_path(ref, charts[ref.ns])
        if not path:
            unplaced.append(f"{ref.ns} {ref.pod}/{ref.container} (chart {charts[ref.ns].name})")
            continue
        file = repos / stage.clone / stage.values_file
        text = texts.setdefault(file, file.read_text())
        texts[file], changes = revise(text, path, rec, args.reset)
        if changes:
            notes[stage.clone].append(
                f"{ref.ns} {ref.pod}/{ref.container}: {', '.join(changes)} "
                f"(measured {rec.cpu}m, {rec.memory}Mi)")
            if stage.revision != ENTRY_BRANCH:
                promoted[stage.clone].add(
                    f"{ref.ns} tracks {stage.revision}: this reaches it by promotion (argo-cd D34).")

    report.mkdir()
    for file, text in texts.items():
        file.write_text(text)
    for name in sorted(notes):
        diff = git(repos / name, "diff", "--no-color", "--no-ext-diff").stdout
        git(repos / name, "checkout", "--", ".")
        lines = [*notes[name], *sorted(promoted[name])]
        (report / f"{name}.patch").write_text(preamble(name, start, end, lines) + diff)

    (work / "not-placed.txt").write_text("".join(f"{line}\n" for line in unplaced))
    log(f"{len(unplaced)} containers in registry namespaces have requests neither their chart's values "
        f"nor its map place; they are listed in {work / 'not-placed.txt'}")
    log(f"{len(notes)} of {len({s.clone for s in stages.values()})} deploy repos would change; "
        f"the patches are in {report}")
    log(f"delete or edit them, then: {Path(__file__).name} apply {work}")


# ---------------------------------------------------------------------------------------------
# Step two: apply what is left


def cmd_apply(args: argparse.Namespace) -> None:
    work = Path(args.workdir)
    patches = sorted((work / "report").glob("*.patch"))
    if not patches:
        raise Stop(f"no patches in {work / 'report'}")
    clones = {p: work / "repos" / p.stem for p in patches}

    problems = []
    for patch, clone in clones.items():
        if not clone.is_dir():
            problems.append(f"{patch.name}: no clone at {clone}")
        elif git(clone, "rev-list", f"origin/{ENTRY_BRANCH}..HEAD").stdout.strip():
            problems.append(f"{patch.name}: {clone} already carries a commit (apply ran already)")
        elif (r := git(clone, "apply", "--index", "--recount", "--check", str(patch), check=False)).returncode:
            problems.append(f"{patch.name}: {r.stderr.strip()}")
    if problems:
        raise Stop("nothing applied; fix or delete these patches:\n  " + "\n  ".join(problems))

    for patch, clone in clones.items():
        git(clone, "apply", "--index", "--recount", str(patch))
    staged = {patch: git(clone, "diff", "--cached", "--name-only").stdout.split() for patch, clone in clones.items()}
    for patch, names in staged.items():
        for name in names:
            try:
                yaml.safe_load((clones[patch] / name).read_text())
            except yaml.YAMLError as e:
                problems.append(f"{patch.name}: {name} is not YAML after the patch: {e}")
    if problems:
        for clone in clones.values():
            git(clone, "reset", "--hard", "--quiet", "HEAD")
        raise Stop("nothing committed; fix these patches:\n  " + "\n  ".join(problems))

    committed = []
    for patch, clone in clones.items():
        if not staged[patch]:
            log(f"{patch.name} changes nothing; {clone.name} is left as it is")
            continue
        git(clone, "commit", "--quiet", "-m", COMMIT_MESSAGE)
        log(f"{clone.name}:")
        print(git(clone, "--no-pager", "show", "--stat", "--patch", "--no-color", "HEAD").stdout, flush=True)
        committed.append(clone)
    log(f"committed {len(committed)} deploy repos on {ENTRY_BRANCH}; nothing is pushed. To push them:")
    for clone in committed:
        print(f"  git -C {clone} push origin {ENTRY_BRANCH}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["report", "apply"])
    ap.add_argument("workdir", help="scratch directory: the clones and the report")
    ap.add_argument("--registry", default=str(REGISTRY), help="report: ArgoCDDeploy's registry file")
    ap.add_argument(
        "--reset",
        action="store_true",
        help=(
            "report: overwrite existing values even when the new recommendation is lower. Off by "
            "default so recommendations only ratchet upwards. Containers absent from the "
            "measurement window are untouched either way, but a container that merely ran quietly "
            "will be revised down; re-run without --reset afterwards to let genuine peaks climb back."
        ),
    )
    args = ap.parse_args()
    try:
        (cmd_report if args.step == "report" else cmd_apply)(args)
    except Stop as e:
        log(f"STOP: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
