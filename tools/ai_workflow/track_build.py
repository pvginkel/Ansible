#!/usr/bin/env python3
"""Track a specific Jenkins build (and the pipelines it triggers) to completion.

This is a token-cheap replacement for the dozens of MCP / API calls an agent
would otherwise make to answer one question: *did the build I just caused
finish, and did everything it kicked off finish too?*

The build is identified **precisely** — never guessed:

    track_build.py Org/Repo  --hash <sha>      # the commit you pushed
    track_build.py Org/Repo  --buildnr <n>     # an exact build number

With ``--hash`` the script scans the job's recent builds for the run that
checked out that commit (matching the git revision Jenkins records on the
build), waiting up to ``--appear-timeout`` seconds for it to show up.

**Superseded builds.** If the tracked build is aborted because a newer build
superseded it (Jenkins prints ``Superseded by #N`` and ends the run
NOT_BUILT/ABORTED), the script automatically follows the chain to ``#N`` and
says so — both live on stderr and in the final summary.

**Triggered pipelines.** When a build finishes it usually schedules a
downstream pipeline (e.g. a target-state deploy). By default the script then
finds the downstream build that *this* build triggered — matched by the
upstream cause Jenkins records on the downstream run, so it is always the exact
run, never a coincidental neighbour — and waits for it too. A downstream build
that has not started yet is pending in the queue; the script waits for it.
Disable with ``--no-wait-downstream``.

**Already-finished builds** are handled identically and return immediately, so
tracking a build (and its downstream) long after both completed is fast.

Output:
  * Live, state-change-only progress on **stderr** (timestamped).
  * A high-level **summary of every completed build on stdout** (always).
  * The **full console log of any non-SUCCESS build written to disk**, with the
    path named in the summary; successful builds' logs are never dumped.

Exit status: ``0`` all tracked builds succeeded; ``1`` a tracked build did not
succeed; ``3`` an operational problem (auth, build never appeared, etc.).

Auth: Jenkins base URL from ``--url`` or ``$JENKINS_URL``; user from ``--user``
or ``$JENKINS_USER``; API token from the env var named by ``--token-env``
(default ``JENKINS_TOKEN``) — never passed on the command line.
"""

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

# How many recent builds to scan when matching by hash / upstream cause. The
# build we want is always near the head of the list, and some target-state jobs
# have thousands of builds, so an unbounded scan would be wasteful.
_SCAN_RANGE = 40

_SUPERSEDED_RE = re.compile(r"Superseded by #(\d+)")
# `build` step log lines that name a scheduled/started downstream project.
_SCHEDULED_RE = re.compile(
    r"^(?:Scheduling project: |Starting building: )(.+?)(?: #\d+)?$", re.MULTILINE
)


class JenkinsError(Exception):
    """An operational failure talking to Jenkins (auth, missing build, ...)."""


class Jenkins:
    """Minimal read-only Jenkins REST client (stdlib only)."""

    def __init__(self, base_url: str, user: str, token: str):
        self.base = base_url.rstrip("/")
        self._auth = base64.b64encode(f"{user}:{token}".encode()).decode()

    def _open(self, url: str):
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Basic {self._auth}")
        try:
            return urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise JenkinsError(
                    f"authentication failed ({e.code}) — check the user and the "
                    f"token in the env var"
                ) from e
            if e.code == 404:
                raise JenkinsError(f"not found (404): {url}") from e
            raise JenkinsError(f"HTTP {e.code} for {url}") from e
        except urllib.error.URLError as e:
            raise JenkinsError(f"cannot reach Jenkins at {url}: {e.reason}") from e

    def get_json(self, path: str, tree: str | None = None) -> dict:
        params = {"tree": tree} if tree else {}
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        url = f"{self.base}/{path.strip('/')}/api/json{query}"
        with self._open(url) as resp:
            return json.load(resp)

    def get_console(self, path: str) -> str:
        url = f"{self.base}/{path.strip('/')}/consoleText"
        with self._open(url) as resp:
            return resp.read().decode("utf-8", "replace")

    def get_queue(self, tree: str) -> dict:
        url = f"{self.base}/queue/api/json?{urllib.parse.urlencode({'tree': tree})}"
        with self._open(url) as resp:
            return json.load(resp)


def job_to_url(job_path: str) -> str:
    """``Org/Repo`` -> ``job/Org/job/Repo`` (Jenkins folder URL form)."""
    parts = [p for p in job_path.strip("/").split("/") if p]
    if not parts:
        raise JenkinsError(f"invalid job path: {job_path!r}")
    return "job/" + "/job/".join(urllib.parse.quote(p) for p in parts)


def _build_sha1s(build: dict) -> set[str]:
    """All git revisions Jenkins recorded for a build (across every repo/SCM)."""
    out: set[str] = set()
    for action in build.get("actions", []):
        rev = action.get("lastBuiltRevision")
        if not rev:
            continue
        if rev.get("SHA1"):
            out.add(rev["SHA1"].lower())
        for branch in rev.get("branch") or []:
            if branch.get("SHA1"):
                out.add(branch["SHA1"].lower())
    return out


def _upstream_causes(node: dict) -> list[tuple[str, int]]:
    """``(upstreamProject, upstreamBuild)`` pairs on a build or queue item."""
    out: list[tuple[str, int]] = []
    for action in node.get("actions", []):
        for cause in action.get("causes") or []:
            if cause.get("upstreamProject") is not None:
                out.append((cause["upstreamProject"], cause.get("upstreamBuild")))
    return out


def _fmt_duration(build: dict) -> str:
    seconds = int((build.get("duration") or 0) // 1000)
    return f"{seconds // 60}m{seconds % 60:02d}s"


def find_build_by_hash(
    jk: Jenkins, job_url: str, sha: str, appear_timeout: float, poll: float
) -> int:
    """Newest build of the job that checked out a commit starting with ``sha``."""
    prefix = sha.lower()
    tree = f"builds[number,actions[lastBuiltRevision[SHA1,branch[SHA1]]]]{{0,{_SCAN_RANGE}}}"
    deadline = time.monotonic() + appear_timeout
    while True:
        data = jk.get_json(job_url, tree=tree)
        matches = [
            b["number"]
            for b in data.get("builds", [])
            if any(s.startswith(prefix) for s in _build_sha1s(b))
        ]
        if matches:
            return max(matches)
        if time.monotonic() >= deadline:
            raise JenkinsError(
                f"no build checking out commit {sha} appeared within {appear_timeout:g}s"
            )
        time.sleep(poll)


def wait_for_completion(jk: Jenkins, job_url: str, number: int, poll: float, log) -> dict:
    """Block until build ``number`` finishes; return its full status dict."""
    tree = "number,building,result,duration,url,fullDisplayName"
    announced = False
    while True:
        build = jk.get_json(f"{job_url}/{number}", tree=tree)
        if build.get("building") is False and build.get("result") is not None:
            return build
        if not announced:
            log(f"{build.get('fullDisplayName', '#' + str(number))} building…")
            announced = True
        time.sleep(poll)


def find_superseded(jk: Jenkins, job_url: str, number: int) -> int | None:
    match = _SUPERSEDED_RE.search(jk.get_console(f"{job_url}/{number}"))
    return int(match.group(1)) if match else None


def track_with_supersede(
    jk: Jenkins, job_url: str, number: int, poll: float, log
) -> tuple[dict, list[str]]:
    """Wait for a build, following any ``Superseded by`` chain to the live run."""
    events: list[str] = []
    while True:
        build = wait_for_completion(jk, job_url, number, poll, log)
        if build.get("result") in ("ABORTED", "NOT_BUILT"):
            successor = find_superseded(jk, job_url, number)
            if successor:
                log(f"#{number} {build['result']} — superseded by #{successor}, switching")
                events.append(f"#{number} superseded by #{successor}")
                number = successor
                continue
        return build, events


def discover_downstream_jobs(console: str) -> list[str]:
    """Full names of the projects a build scheduled, from its console log."""
    names: list[str] = []
    for match in _SCHEDULED_RE.finditer(console):
        # Jenkins prints folder paths with " » " (U+00BB); full names use "/".
        full = re.sub(r"\s*»\s*", "/", match.group(1).strip())
        if full and full not in names:
            names.append(full)
    return names


def find_downstream_build(
    jk: Jenkins,
    ds_url: str,
    ds_name: str,
    upstream: str,
    upstream_number: int,
    trigger_timeout: float,
    poll: float,
    log,
) -> int:
    """Build number in ``ds_url`` triggered by ``upstream`` #``upstream_number``."""
    want = (upstream, upstream_number)
    builds_tree = (
        f"builds[number,actions[causes[upstreamProject,upstreamBuild]]]{{0,{_SCAN_RANGE}}}"
    )
    queue_tree = (
        "items[task[url],executable[number],actions[causes[upstreamProject,upstreamBuild]]]"
    )
    deadline = time.monotonic() + trigger_timeout
    was_queued = False
    while True:
        for build in jk.get_json(ds_url, tree=builds_tree).get("builds", []):
            if want in _upstream_causes(build):
                return build["number"]

        # Not a build yet — it may be waiting in the queue.
        queued = False
        for item in jk.get_queue(queue_tree).get("items", []):
            if want in _upstream_causes(item):
                executable = item.get("executable") or {}
                if executable.get("number"):
                    return executable["number"]
                queued = True
                break

        if queued:
            # --trigger-timeout bounds DISCOVERY only. A build sitting in the queue has
            # demonstrably been triggered, and how long it then waits for a free executor is
            # the controller's business — on a busy Jenkins that is routinely longer than the
            # timeout, and killing the tracker there reports a perfectly healthy build as
            # never-triggered. So push the deadline out for as long as we can still see it
            # queued — and no longer, so a CANCELLED queue item still fails below instead of
            # polling forever.
            if not was_queued:
                log(f"{ds_name} (triggered by #{upstream_number}) is pending in queue…")
                was_queued = True
            deadline = time.monotonic() + trigger_timeout

        if time.monotonic() >= deadline:
            if was_queued:
                raise JenkinsError(
                    f"the {ds_name} build triggered by {upstream} #{upstream_number} left the "
                    f"queue without starting — cancelled?"
                )
            raise JenkinsError(
                f"no build triggered by {upstream} #{upstream_number} appeared in "
                f"{ds_name} within {trigger_timeout:g}s"
            )
        time.sleep(poll)


def dump_failure_log(
    jk: Jenkins, job_url: str, name: str, build: dict, log_dir: str
) -> tuple[str, str]:
    """Write the failing build's console to disk; return (path, console text)."""
    os.makedirs(log_dir, exist_ok=True)
    safe = name.replace("/", "_")
    path = os.path.join(log_dir, f"{safe}_{build['number']}.log")
    console = jk.get_console(f"{job_url}/{build['number']}")
    with open(path, "w") as fh:
        fh.write(console)
    return path, console


def failure_tail(console: str, lines: int = 60) -> str:
    """The last ``lines`` non-empty console lines — where Jenkins failures live."""
    tail = [line for line in console.splitlines() if line.strip()]
    return "\n".join(tail[-lines:])


def make_logger():
    def log(message: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)

    return log


def print_summary(
    rows: list[tuple[str, dict, str | None]], events: list[str], op_error: str | None
) -> None:
    name_w = max((len(name) for name, _, _ in rows), default=0)
    num_w = max((len(f"#{b['number']}") for _, b, _ in rows), default=2)
    print("=== Build tracking summary ===")
    for name, build, logfile in rows:
        print(
            f"{name:<{name_w}}  {'#' + str(build['number']):<{num_w}}  "
            f"{build.get('result', '?'):<8}  {_fmt_duration(build):>7}  "
            f"{build.get('url', '')}"
        )
        if logfile:
            print(f"{'':<{name_w}}  {'':<{num_w}}  ↳ full log: {logfile}")
    if events:
        print("\nEvents:")
        for event in events:
            print(f"  - {event}")
    print()
    failures = [r for r in rows if r[1].get("result") != "SUCCESS"]
    if op_error:
        print(f"Result: tracking incomplete — {op_error}")
    elif failures:
        print(f"Result: {len(failures)} of {len(rows)} tracked build(s) did NOT succeed.")
    else:
        print(f"Result: all {len(rows)} tracked build(s) succeeded.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Track a precise Jenkins build (and what it triggers) to completion.",
    )
    parser.add_argument("job", help="job path, slash-separated, e.g. Org/Repo or TopLevelJob")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--hash", metavar="SHA", help="git commit (full or prefix) the build checked out"
    )
    target.add_argument("--buildnr", type=int, metavar="N", help="exact build number")
    parser.add_argument(
        "--wait-downstream",
        dest="wait_downstream",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="also track pipelines this build triggers (default: on)",
    )
    parser.add_argument(
        "--url", default=os.environ.get("JENKINS_URL", "https://jenkins.webathome.org")
    )
    parser.add_argument("--user", default=os.environ.get("JENKINS_USER", "admin"))
    parser.add_argument(
        "--token-env", default="JENKINS_TOKEN", help="env var holding the API token"
    )
    parser.add_argument("--poll-interval", type=float, default=5.0, metavar="SECONDS")
    parser.add_argument(
        "--appear-timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="how long to wait for a --hash build to appear (default: 30)",
    )
    parser.add_argument(
        "--trigger-timeout",
        type=float,
        default=120.0,
        metavar="SECONDS",
        help="how long to wait for a triggered downstream build to be FOUND — as a build or "
        "as a queue item (default: 120). Once found in the queue it is tracked however "
        "long it waits for an executor.",
    )
    parser.add_argument(
        "--log-dir",
        default=os.path.join(tempfile.gettempdir(), "track_build"),
        help="where to write failing builds' console logs",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="on failure, print the failing build's console tail to stdout "
        "(so the caller never has to read the raw log)",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    token = os.environ.get(args.token_env)
    if not token:
        print(f"error: env var ${args.token_env} is not set (Jenkins API token)", file=sys.stderr)
        return 3

    jk = Jenkins(args.url, args.user, token)
    log = make_logger()
    job_url = job_to_url(args.job)
    upstream_name = jk.get_json(job_url, tree="fullName")["fullName"]

    if args.hash:
        log(f"Resolving {args.job} build for commit {args.hash}…")
        number = find_build_by_hash(jk, job_url, args.hash, args.appear_timeout, args.poll_interval)
        log(f"commit {args.hash} → {args.job} #{number}")
    else:
        number = args.buildnr
        log(f"Tracking {args.job} #{number}")

    primary, events = track_with_supersede(jk, job_url, number, args.poll_interval, log)
    log(f"{args.job} #{primary['number']} {primary['result']} ({_fmt_duration(primary)})")

    tracked: list[tuple[str, str, dict]] = [(args.job, job_url, primary)]
    op_error: str | None = None

    if args.wait_downstream:
        console = jk.get_console(f"{job_url}/{primary['number']}")
        for ds_name in discover_downstream_jobs(console):
            log(f"Discovered downstream pipeline: {ds_name}")
            ds_url = job_to_url(ds_name)
            try:
                ds_number = find_downstream_build(
                    jk,
                    ds_url,
                    ds_name,
                    upstream_name,
                    primary["number"],
                    args.trigger_timeout,
                    args.poll_interval,
                    log,
                )
                ds_build = wait_for_completion(jk, ds_url, ds_number, args.poll_interval, log)
            except JenkinsError as exc:
                log(f"error tracking downstream {ds_name}: {exc}")
                op_error = str(exc)
                break
            log(f"{ds_name} #{ds_build['number']} {ds_build['result']} ({_fmt_duration(ds_build)})")
            tracked.append((ds_name, ds_url, ds_build))

    rows: list[tuple[str, dict, str | None]] = []
    diagnoses: list[tuple[str, dict, str]] = []
    any_failed = False
    for name, url, build in tracked:
        logfile = None
        if build.get("result") != "SUCCESS":
            any_failed = True
            logfile, console = dump_failure_log(jk, url, name, build, args.log_dir)
            log(f"{name} #{build['number']} not SUCCESS — full log: {logfile}")
            if args.diagnose:
                diagnoses.append((name, build, failure_tail(console)))
        rows.append((name, build, logfile))

    print_summary(rows, events, op_error)
    for name, build, tail in diagnoses:
        print(f"\n=== Failure tail: {name} #{build['number']} ({build.get('result')}) ===")
        print(tail)
    if op_error:
        return 3
    return 1 if any_failed else 0


def main() -> int:
    try:
        return run(parse_args())
    except JenkinsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
