"""recommend-resources: the policy, where a container's requests go, the values-file edits, and
the report -> apply round trip on local git repositories.

Run: python3 -m unittest discover -s support/recommend-resources
"""

import argparse
import contextlib
import io
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import yaml

import recommend_resources as rr
from recommend_resources import Chart, ContainerRef, Recommendation


class Policy(unittest.TestCase):
    def test_percentile_is_numpys_linear(self):
        self.assertEqual(rr.percentile([4, 1, 3, 2], 75), 3.25)
        self.assertAlmostEqual(rr.percentile([float(i) for i in range(1, 11)], 90), 9.1)
        self.assertEqual(rr.percentile([5.0], 90), 5.0)

    def test_workload_from_pod(self):
        self.assertEqual(rr.infer_workload("calendar-support-6d9f8b7c5d-x2x9z"), "calendar-support")
        self.assertEqual(rr.infer_workload("step-ca-0"), "step-ca")
        # The maps' keys depend on this: an 8-10 char last word reads as a ReplicaSet hash.
        self.assertEqual(rr.infer_workload("prometheus-prd-prometheus-node-exporter-9wrrz"),
                         "prometheus-prd-prometheus-node")

    def test_rounding(self):
        self.assertEqual([rr.round_cpu_recommendation(v) for v in (9, 13, 371)], [0, 20, 400])
        self.assertEqual([rr.round_mem_recommendation(v) for v in (0, 100, 118, 129)], [0, 112, 128, 160])

    def test_window_and_percentiles(self):
        series = {
            "cpu": [{"metric": {"namespace": "a-prd", "pod": "a-0", "container": "c"},
                     "values": [[0, "0.010"], [1, "0.020"], [2, "0.030"], [3, "0.050"]]}],
            "mem": [{"metric": {"namespace": "a-prd", "pod": "a-0", "container": "c"},
                     "values": [[0, str(v * 1024 * 1024)] for v in range(1, 11)]}],
        }
        calls = []

        def promql(query, start, end, step="300s"):
            calls.append((start, end, step))
            return series["cpu" if "cpu" in query else "mem"]

        start = rr.datetime(2026, 9, 19, tzinfo=rr.timezone.utc)
        end = start + rr.timedelta(days=rr.NUM_DAYS)
        with mock.patch.object(rr, "promql", promql):
            recs = rr.get_recommendations(start, end)
        self.assertEqual(calls, [(int(start.timestamp()), int(end.timestamp()), "300s")] * 2)
        self.assertEqual(recs, {ContainerRef("a-prd", "a", "c"): Recommendation(35, 9)})


class ResolvedChart(unittest.TestCase):
    REGISTRY = {"apps": {
        "calendar-support": {"repo": "https://github.com/pvginkel/CalendarSupportDeploy.git",
                             "stages": {"prd": {}}},
        "kubecoder": {"repo": "https://github.com/pvginkel/KubeCoderDeploy.git",
                      "stages": {"dev": {}, "prd": {"targetRevision": "prd"}}},
        "step-ca": {"repo": "https://github.com/pvginkel/StepCaDeploy.git",
                    "upstream": {"repo": "https://smallstep.github.io/helm-charts", "chart": "step-certificates"},
                    "stages": {"prd": {"version": "1.30.1"}}},
    }}

    def test_registry_stages(self):
        stages = rr.registry_stages(self.REGISTRY)
        self.assertEqual(sorted(stages), ["calendar-support-prd", "kubecoder-dev", "kubecoder-prd", "step-ca-prd"])
        self.assertEqual(stages["kubecoder-dev"].revision, "main")
        self.assertEqual(stages["kubecoder-prd"].revision, "prd")
        self.assertEqual(stages["kubecoder-prd"].values_file, "config/prd/values.yaml")
        self.assertEqual(stages["step-ca-prd"].clone, "StepCaDeploy")
        self.assertEqual(stages["step-ca-prd"].version, "1.30.1")

    def test_upstream_app_resolves_to_the_registry_chart_at_its_pin(self):
        stage = rr.registry_stages(self.REGISTRY)["step-ca-prd"]
        with mock.patch.object(rr, "run") as run:
            run.return_value.stdout = "resources: {}\n"
            chart = rr.resolve_chart(stage, Path("/nonexistent"))
        self.assertEqual(chart, Chart("step-certificates", {"resources": {}}))
        self.assertEqual(run.call_args.args[0][-5:], ["step-certificates", "--repo",
                                                      "https://smallstep.github.io/helm-charts",
                                                      "--version", "1.30.1"])

    def test_local_app_resolves_to_the_deploy_repos_chart(self):
        stage = rr.registry_stages(self.REGISTRY)["calendar-support-prd"]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "chart").mkdir()
            (Path(tmp) / "chart/Chart.yaml").write_text("name: calendar-support\n")
            (Path(tmp) / "chart/values.yaml").write_text("resources:\n  a:\n    b: {}\n")
            self.assertEqual(rr.resolve_chart(stage, Path(tmp)),
                             Chart("calendar-support", {"resources": {"a": {"b": {}}}}))

    def test_path_from_the_charts_values_first(self):
        chart = Chart("calendar-support", {"resources": {"calendar-support": {"app": {}}}})
        self.assertEqual(rr.get_resources_path(ContainerRef("calendar-support-prd", "calendar-support", "app"), chart),
                         ["resources", "calendar-support", "app", "requests"])
        self.assertIsNone(rr.get_resources_path(ContainerRef("calendar-support-prd", "other", "app"), chart))

    def test_map_keyed_on_the_resolved_chart_not_the_app(self):
        ref = ContainerRef("step-ca-prd", "step-ca", "step-certificates")
        self.assertEqual(rr.get_resources_path(ref, Chart("step-certificates", {"resources": {}})),
                         ["resources", "requests"])
        self.assertIsNone(rr.get_resources_path(ref, Chart("step-ca", {})))

    def test_every_map_names_a_requests_path(self):
        maps = sorted(rr.MAPS.glob("*.json"))
        self.assertEqual(len(maps), 9)
        for m in maps:
            for key, path in rr.entry_map(m.stem).items():
                self.assertRegex(key, r"^[a-z0-9-]+/[a-z0-9-]+$", m.name)
                self.assertTrue(path.endswith(".requests"), f"{m.name}: {path}")


class Edit(unittest.TestCase):
    VALUES = textwrap.dedent("""\
        # Stage values.
        image:
          tag: "1.2"

        resources:
          app:
            # The main container.
            app:
              requests:
                memory: "112Mi"  # measured
            sidecar:
              requests:
                cpu: 10m
                memory: 16Mi

        externalSecrets:
          enabled: true
        """)

    def test_existing_value_replaced_in_place_keeping_quotes_and_comment(self):
        out = rr.set_value(self.VALUES, ["resources", "app", "app", "requests", "memory"], "128Mi")
        self.assertEqual(out, self.VALUES.replace('memory: "112Mi"  # measured', 'memory: "128Mi"  # measured'))

    def test_missing_leaf_appended_to_its_mapping(self):
        out = rr.set_value(self.VALUES, ["resources", "app", "app", "requests", "cpu"], "20m")
        self.assertEqual(out, self.VALUES.replace('# measured\n', '# measured\n        cpu: 20m\n'))

    def test_missing_mappings_added_at_the_end_of_the_deepest_one(self):
        out = rr.set_value(self.VALUES, ["resources", "worker", "worker", "requests", "cpu"], "30m")
        self.assertEqual(out, self.VALUES.replace(
            "        memory: 16Mi\n",
            "        memory: 16Mi\n  worker:\n    worker:\n      requests:\n        cpu: 30m\n"))
        self.assertEqual(yaml.safe_load(out)["resources"]["worker"], {"worker": {"requests": {"cpu": "30m"}}})

    def test_new_top_level_key_gets_a_blank_line(self):
        text = "image:\n  tag: x\n"
        self.assertEqual(rr.set_value(text, ["server", "resources", "requests", "cpu"], "20m"),
                         "image:\n  tag: x\n\nserver:\n  resources:\n    requests:\n      cpu: 20m\n")
        self.assertEqual(rr.set_value("", ["resources", "requests", "cpu"], "20m"),
                         "resources:\n  requests:\n    cpu: 20m\n")

    def test_empty_and_null_values_become_blocks(self):
        for empty in (" {}", "", " ~"):
            with self.subTest(empty=empty):
                text = f"resources:{empty}\n  # upstream's comment\nnext: 1\n"
                self.assertEqual(rr.set_value(text, ["resources", "requests", "cpu"], "20m"),
                                 "resources:\n  requests:\n    cpu: 20m\n  # upstream's comment\nnext: 1\n")

    def test_flow_mapping_stays_flow(self):
        text = "resources:\n  requests: {cpu: 10m, memory: 64Mi}\n"
        self.assertEqual(rr.set_value(text, ["resources", "requests", "memory"], "80Mi"),
                         "resources:\n  requests: {cpu: 10m, memory: 80Mi}\n")

    def test_scalar_on_the_path_refused(self):
        with self.assertRaises(rr.Stop):
            rr.set_value("resources: none\n", ["resources", "requests", "cpu"], "20m")

    def test_delete_drops_the_line(self):
        out = rr.delete_value(self.VALUES, ["resources", "app", "sidecar", "requests", "cpu"])
        self.assertEqual(out, self.VALUES.replace("        cpu: 10m\n", ""))

    def test_delete_of_the_last_key_leaves_an_empty_mapping(self):
        out = rr.delete_value(self.VALUES, ["resources", "app", "app", "requests", "memory"])
        self.assertEqual(out, self.VALUES.replace('requests:\n        memory: "112Mi"  # measured\n', "requests: {}\n"))
        self.assertEqual(yaml.safe_load(out)["resources"]["app"]["app"], {"requests": {}})

    def test_delete_of_an_absent_key_changes_nothing(self):
        self.assertEqual(rr.delete_value(self.VALUES, ["resources", "nope", "requests", "cpu"]), self.VALUES)


class Revise(unittest.TestCase):
    PATH = ["resources", "a", "a", "requests"]
    TEXT = "resources:\n  a:\n    a:\n      requests:\n        cpu: 200m\n        memory: 64Mi\n"

    def test_only_raises(self):
        text, changes = rr.revise(self.TEXT, self.PATH, Recommendation(cpu=13, memory=100), reset=False)
        self.assertEqual(text, self.TEXT.replace("64Mi", "112Mi"))
        self.assertEqual(changes, ["memory 64Mi -> 112Mi"])

    def test_nothing_higher_nothing_changed(self):
        self.assertEqual(rr.revise(self.TEXT, self.PATH, Recommendation(cpu=13, memory=20), reset=False),
                         (self.TEXT, []))

    def test_reset_lowers_and_drops_a_request_under_10m(self):
        text, changes = rr.revise(self.TEXT, self.PATH, Recommendation(cpu=4, memory=20), reset=True)
        self.assertEqual(text, "resources:\n  a:\n    a:\n      requests:\n        memory: 20Mi\n")
        self.assertEqual(changes, ["cpu 200m -> unset", "memory 64Mi -> 20Mi"])

    def test_adds_requests_the_file_lacks(self):
        text, changes = rr.revise("image: x\n", self.PATH, Recommendation(cpu=13, memory=100), reset=False)
        self.assertEqual(yaml.safe_load(text)["resources"], {"a": {"a": {"requests": {"cpu": "20m", "memory": "112Mi"}}}})
        self.assertEqual(changes, ["cpu unset -> 20m", "memory unset -> 112Mi"])


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout


GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


@mock.patch.dict(os.environ, GIT_ENV)
class RoundTrip(unittest.TestCase):
    """report writes one patch per deploy repo that would change; the operator deletes one and
    edits another; apply commits what is left, on main, and pushes nothing."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(sh, "rm", "-rf", str(self.tmp))
        origins = self.tmp / "origins"
        self.origins = {}
        for name, chart_values, stage_values in (
            ("AlphaDeploy", "resources:\n  alpha:\n    app: {}\n", "image: a\n"),
            ("BetaDeploy", "resources:\n  beta:\n    app: {}\n",
             "resources:\n  beta:\n    app:\n      requests:\n        memory: 64Mi\n"),
            ("GammaDeploy", "resources:\n  gamma:\n    app: {}\n", "image: g\n"),
            ("QuietDeploy", "resources:\n  quiet:\n    app: {}\n",
             "resources:\n  quiet:\n    app:\n      requests:\n        cpu: 900m\n        memory: 4Gi\n"),
        ):
            repo = origins / f"{name}.git"
            (repo / "chart").mkdir(parents=True)
            (repo / "config/prd").mkdir(parents=True)
            (repo / "chart/Chart.yaml").write_text(f"name: {name.removesuffix('Deploy').lower()}\n")
            (repo / "chart/values.yaml").write_text(chart_values)
            (repo / "config/prd/values.yaml").write_text(stage_values)
            sh("git", "init", "-q", "-b", "main", cwd=repo)
            sh("git", "add", ".", cwd=repo)
            sh("git", "commit", "-q", "-m", "init", cwd=repo)
            self.origins[name] = repo
        self.registry = self.tmp / "registry.yaml"
        apps = {name.removesuffix("Deploy").lower(): {"repo": f"file://{repo}", "stages": {"prd": {}}}
                for name, repo in self.origins.items()}
        apps["beta"]["stages"]["prd"]["targetRevision"] = "prd"
        self.registry.write_text(yaml.safe_dump({"apps": apps}))
        self.work = self.tmp / "work"

    def metrics(self, query, start, end, step="300s"):
        def series(ns, pod, container, value):
            return {"metric": {"namespace": ns, "pod": pod, "container": container}, "values": [[0, str(value)]]}
        value = 0.013 if "cpu" in query else 100 * 1024 * 1024
        return [
            *(series(f"{w}-prd", f"{w}-0", "app", value) for w in ("alpha", "beta", "gamma", "quiet")),
            series("alpha-prd", "alpha-0", "sidecar", value),
            series("elsewhere", "other-0", "app", value),
        ]

    def step(self, name, **kw):
        args = argparse.Namespace(workdir=str(self.work), registry=str(self.registry), reset=False, **kw)
        out = io.StringIO()
        with mock.patch.object(rr, "promql", self.metrics), contextlib.redirect_stdout(out):
            getattr(rr, f"cmd_{name}")(args)
        return out.getvalue()

    def test_report_edit_apply(self):
        self.step("report")
        report = self.work / "report"
        self.assertEqual(sorted(p.name for p in report.iterdir()),
                         ["AlphaDeploy.patch", "BetaDeploy.patch", "GammaDeploy.patch"])
        beta = (report / "BetaDeploy.patch").read_text()
        self.assertIn("# beta-prd beta/app: cpu unset -> 20m, memory 64Mi -> 112Mi (measured 13m, 100Mi)\n"
                      "# beta-prd tracks prd: this reaches it by promotion (argo-cd D34).\n", beta)
        self.assertIn("+        cpu: 20m\n", beta)
        self.assertEqual((self.work / "not-placed.txt").read_text(), "alpha-prd alpha/sidecar (chart alpha)\n")
        # The clones stay at the origin's commit until apply.
        self.assertEqual(sh("git", "status", "--porcelain", cwd=self.work / "repos/BetaDeploy"), "")

        # The operator skips Gamma and overrules Beta's CPU.
        (report / "GammaDeploy.patch").unlink()
        (report / "BetaDeploy.patch").write_text(beta.replace("+        cpu: 20m\n", "+        cpu: 50m\n"))
        out = self.step("apply")
        self.assertIn("git -C", out)

        beta_clone = self.work / "repos/BetaDeploy"
        self.assertEqual(yaml.safe_load((beta_clone / "config/prd/values.yaml").read_text()),
                         {"resources": {"beta": {"app": {"requests": {"memory": "112Mi", "cpu": "50m"}}}}})
        self.assertEqual(sh("git", "rev-list", "--count", "origin/main..main", cwd=beta_clone), "1\n")
        self.assertEqual(sh("git", "rev-list", "--count", "origin/main..main", cwd=self.work / "repos/GammaDeploy"), "0\n")
        # Nothing reached the origins.
        for repo in self.origins.values():
            self.assertEqual(sh("git", "rev-list", "--count", "main", cwd=repo), "1\n")

        with self.assertRaisesRegex(rr.Stop, "apply ran already"):
            self.step("apply")

    def test_a_broken_patch_applies_nothing(self):
        self.step("report")
        report = self.work / "report"
        alpha = (report / "AlphaDeploy.patch").read_text()
        (report / "AlphaDeploy.patch").write_text(alpha.replace(" image: a\n", " image: zzz\n"))
        with self.assertRaisesRegex(rr.Stop, "AlphaDeploy.patch"):
            self.step("apply")
        for name in ("AlphaDeploy", "BetaDeploy", "GammaDeploy"):
            clone = self.work / "repos" / name
            self.assertEqual(sh("git", "status", "--porcelain", cwd=clone), "")
            self.assertEqual(sh("git", "rev-list", "--count", "origin/main..main", cwd=clone), "0\n")

    def test_report_refuses_an_existing_workdir(self):
        self.work.mkdir()
        with self.assertRaisesRegex(rr.Stop, "fresh clones"):
            self.step("report")


if __name__ == "__main__":
    unittest.main()
