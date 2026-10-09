"""Each srviac task reaches srviac with one command: `plan` and `run` hand `iac` exactly
`-c <script>`, which is all `iac-impl` accepts, and `ui` runs `secret-rotator-ui`.

The command line is built as VS Code builds a shell task's on Linux: `_buildShellCommandLine`
in `src/vs/workbench/contrib/tasks/browser/terminalTaskSystem.ts` (tag 1.105.0) with its bash
quoting options. bash then parses it locally, ssh joins the remote words with spaces, and
srviac's shell parses that again.
"""

import json
import pathlib
import re
import subprocess
import unittest

TASKS = pathlib.Path(__file__).with_name("tasks.json")
LEAF = "iac/rotator-approle"

STRONG, WEAK = "'", '"'
ESCAPED = re.compile(r"[ \"']")


def needs_quotes(value):
    if len(value) >= 2 and value[0] in (STRONG, WEAK) and value[0] == value[-1]:
        return False
    quote = None
    for ch in value:
        if ch == quote:
            quote = None
        elif quote is not None:
            continue
        elif ch in (STRONG, WEAK):
            quote = ch
        elif ch == " ":
            return True
    return False


def quote_if_necessary(value):
    if isinstance(value, str):
        return f"{STRONG}{value}{STRONG}" if needs_quotes(value) else value
    kind, text = value["quoting"], value["value"]
    if kind == "strong":
        return f"{STRONG}{text}{STRONG}"
    if kind == "weak":
        return f"{WEAK}{text}{WEAK}"
    return ESCAPED.sub(lambda m: "\\" + m.group(0), text)


def resolve(value):
    if isinstance(value, str):
        return value.replace("${input:secretRotatorLeaf}", LEAF)
    return {**value, "value": resolve(value["value"])}


def command_line(task):
    original = task["command"]
    command = resolve(original)
    args = [resolve(a) for a in task.get("args", [])]
    if not args and isinstance(command, str) and (command == original or needs_quotes(original)):
        return command
    return " ".join(quote_if_necessary(v) for v in [command, *args])


def words(line):
    out = subprocess.run(
        ["bash", "-c", "printf '%s\\0' " + line], capture_output=True, check=True, text=True
    ).stdout
    return out.split("\0")[:-1]


def srviac_tasks():
    text = "".join(line for line in TASKS.read_text().splitlines(keepends=True)
                   if not line.lstrip().startswith("//"))
    return [t for t in json.loads(text)["tasks"] if t["label"].endswith(" (srviac)")]


REMOTE = {
    "plan": ["sudo", "iac", "-c", f"secret-rotator plan {LEAF}"],
    "run": ["sudo", "iac", "-c", f"secret-rotator run {LEAF}"],
    "ui": ["secret-rotator-ui"],
}


class SrviacTasks(unittest.TestCase):
    def test_each_task_reaches_srviac_with_one_command(self):
        tasks = srviac_tasks()
        self.assertEqual(
            sorted(t["label"] for t in tasks),
            [f"secret-rotator {verb} (srviac)" for verb in REMOTE],
        )
        for task in tasks:
            verb = task["label"].split()[1]
            with self.subTest(task["label"]):
                local = words(command_line(task))
                self.assertEqual(local[:3], ["ssh", "-t", "ansible@srviac"])
                self.assertEqual(words(" ".join(local[3:])), REMOTE[verb])


if __name__ == "__main__":
    unittest.main()
