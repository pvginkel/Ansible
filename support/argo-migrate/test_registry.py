"""flip's and autosync's edits of the registry text (argo_migrate.registry_flip, registry_autosync).

Run: python3 -m unittest discover -s support/argo-migrate
"""

import unittest

import yaml

from argo_migrate import Stop, registry_autosync, registry_flip

# The registry's shapes (ArgoCDDeploy releases/values.yaml): a commented first entry, an upstream
# app with syncOptions, one with a single pinned stage, two stages with a comment between them,
# and a last entry with a flow-style stage.
REGISTRY = """\
# The registry (D63): every app-stage Argo CD deploys.
apps:
  # Argo CD's own entry (D3).
  argocd:
    repo: https://github.com/pvginkel/ArgoCDDeploy.git
    stages:
      prd:
        # Permanent (D3).
        autoSync: false
  cloudnative-pg:
    repo: https://github.com/pvginkel/CloudnativePgDeploy.git
    upstream:
      repo: https://cloudnative-pg.github.io/charts
      chart: cloudnative-pg
    # Its CRDs exceed the 256 KiB annotation (D62).
    syncOptions:
      - ServerSideApply=true
    stages:
      prd:
        version: "0.29.1"
  grafana:
    repo: https://github.com/pvginkel/GrafanaDeploy.git
    upstream:
      repo: https://grafana.github.io/helm-charts
      chart: grafana
    stages:
      prd:
        version: "10.5.15"
  kubecoder:
    repo: https://github.com/pvginkel/KubeCoderDeploy.git
    stages:
      dev: {}
      # D34: prd tracks the `prd` branch.
      prd:
        targetRevision: prd
  media:
    repo: https://github.com/pvginkel/MediaDeploy.git
    stages:
      prd: {}
"""

GRAFANA = """\
  grafana:
    repo: https://github.com/pvginkel/GrafanaDeploy.git
    upstream:
      repo: https://grafana.github.io/helm-charts
      chart: grafana
    stages:
      prd:
        version: "10.5.15"
"""
GRAFANA_ENTRY = {"repo": "https://github.com/pvginkel/GrafanaDeploy.git",
                 "upstream": {"repo": "https://grafana.github.io/helm-charts", "chart": "grafana"}}
MEDIA = """\
  media:
    repo: https://github.com/pvginkel/MediaDeploy.git
    stages:
      prd: {}
"""
MEDIA_ENTRY = {"repo": "https://github.com/pvginkel/MediaDeploy.git"}
KUBECODER_ENTRY = {"repo": "https://github.com/pvginkel/KubeCoderDeploy.git"}


def without(text: str, part: str) -> str:
    assert text.count(part) == 1, part
    return text.replace(part, "")


def comments(text: str) -> list[str]:
    return [l for l in text.splitlines() if l.lstrip().startswith("#")]


class Flip(unittest.TestCase):
    def test_new_app_lands_in_name_order_and_autosync_restores_the_file(self):
        before = without(REGISTRY, GRAFANA)
        flipped = registry_flip(before, "grafana", "prd", GRAFANA_ENTRY,
                                {"autoSync": False, "version": "10.5.15"})
        self.assertEqual(flipped, REGISTRY.replace(
            '      prd:\n        version: "10.5.15"\n',
            '      prd:\n        autoSync: false\n        version: "10.5.15"\n'))
        self.assertEqual(registry_autosync(flipped, "grafana", "prd"), REGISTRY)

    def test_new_last_app_goes_at_the_end(self):
        before = without(REGISTRY, MEDIA)
        flipped = registry_flip(before, "media", "prd", MEDIA_ENTRY, {"autoSync": False})
        self.assertEqual(flipped, before + "  media:\n    repo: https://github.com/pvginkel/MediaDeploy.git\n"
                                           "    stages:\n      prd:\n        autoSync: false\n")
        self.assertEqual(registry_autosync(flipped, "media", "prd"), REGISTRY)

    def test_new_first_app_goes_above_the_next_entrys_comments(self):
        flipped = registry_flip(REGISTRY, "aaa", "prd", {"repo": "https://github.com/pvginkel/AaaDeploy.git"},
                                {"autoSync": False})
        self.assertIn("apps:\n  aaa:\n    repo: https://github.com/pvginkel/AaaDeploy.git\n    stages:\n"
                      "      prd:\n        autoSync: false\n  # Argo CD's own entry (D3).\n  argocd:\n", flipped)
        self.assertEqual(comments(flipped), comments(REGISTRY))

    def test_new_app_with_sync_options_carries_them_and_their_reason(self):
        entry = {"repo": "https://github.com/pvginkel/ExternalSecretsDeploy.git",
                 "upstream": {"repo": "https://charts.external-secrets.io", "chart": "external-secrets"},
                 "syncOptions": ["ServerSideApply=true"]}
        flipped = registry_flip(REGISTRY, "external-secrets", "prd", entry,
                                {"autoSync": False, "version": "2.11.0"})
        apps = yaml.safe_load(flipped)["apps"]
        self.assertEqual(list(apps), ["argocd", "cloudnative-pg", "external-secrets", "grafana", "kubecoder",
                                      "media"])
        self.assertEqual(apps["external-secrets"],
                         {**entry, "stages": {"prd": {"autoSync": False, "version": "2.11.0"}}})
        self.assertIn("    # annotation that client-side apply writes (D62).\n    syncOptions:\n"
                      "      - ServerSideApply=true\n", flipped)

    def test_a_further_stage_joins_the_apps_stages_above_the_next_stages_comment(self):
        before = without(REGISTRY, "      dev: {}\n")
        flipped = registry_flip(before, "kubecoder", "dev", KUBECODER_ENTRY, {"autoSync": False})
        self.assertIn("    stages:\n      dev:\n        autoSync: false\n      # D34: prd tracks", flipped)
        self.assertEqual(registry_autosync(flipped, "kubecoder", "dev"), REGISTRY)

    def test_a_stage_the_registry_has_stops(self):
        with self.assertRaisesRegex(Stop, "kubecoder-prd is there already"):
            registry_flip(REGISTRY, "kubecoder", "prd", KUBECODER_ENTRY, {"autoSync": False})

    def test_a_stage_whose_app_entry_differs_stops(self):
        with self.assertRaisesRegex(Stop, "kubecoder's syncOptions differ"):
            registry_flip(REGISTRY, "kubecoder", "test", {**KUBECODER_ENTRY, "syncOptions": ["ServerSideApply=true"]},
                          {"autoSync": False})


class Autosync(unittest.TestCase):
    def test_the_autosync_line_goes_with_its_comment_and_the_key_keeps_its_own(self):
        text = REGISTRY.replace("      prd: {}\n", "      prd:  # media's only stage\n        autoSync: false\n")
        self.assertEqual(registry_autosync(text, "media", "prd"),
                         REGISTRY.replace("      prd: {}\n", "      prd: {}  # media's only stage\n"))
        synced = registry_autosync(REGISTRY, "argocd", "prd")
        self.assertIn("  argocd:\n    repo: https://github.com/pvginkel/ArgoCDDeploy.git\n    stages:\n"
                      "      prd: {}\n  cloudnative-pg:\n", synced)

    def test_a_stage_the_registry_lacks_stops(self):
        with self.assertRaisesRegex(Stop, "no kubecoder-test; flip comes first"):
            registry_autosync(REGISTRY, "kubecoder", "test")

    def test_a_stage_already_auto_syncing_stops(self):
        with self.assertRaisesRegex(Stop, "grafana-prd auto-syncs already"):
            registry_autosync(REGISTRY, "grafana", "prd")


if __name__ == "__main__":
    unittest.main()
