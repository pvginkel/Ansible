"""annotate: the contract the check holds, what the apply writes and how, and that no value leaks.

Run: python3 -m unittest discover -s scripts/rotation
"""

import copy
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

import annotate as an
import yaml

# Each fixture leaf: its data (every value a SECRET- sentinel) and the annotations that make it
# compliant. Together they exercise each owned-keys rule, copies, per-key intervals and the
# activator forms.
COMPLIANT = {
    "eso/prd/app/prd/oidc": (["client_id", "client_secret"], {
        "rotation_mechanism": "keycloak-client", "rotation_args": '{"realm":"homelab"}',
        "rotation_interval": "14d", "rotation_activate": "auto"}),
    "eso/prd/app/prd/token": (["token"], {
        "rotation_mechanism": "random", "rotation_interval": "14d",
        "rotation_activate": "auto"}),
    "eso/prd/bot/prd/config": (["jenkins-token", "telegram-bot-token", "telegram-chat-id"], {
        "rotation_mechanism": "jenkins-token", "key_telegram-bot-token": "manual",
        "key_telegram-chat-id": "none", "rotation_interval": "14d",
        "interval_telegram-bot-token": "365d", "rotation_activate": "auto"}),
    "eso/prd/es/prd/creds": (["password", "username"], {
        "rotation_mechanism": "elastic-user", "key_username": "none",
        "rotation_args": '{"user":"filebeat_writer"}', "rotation_interval": "14d",
        "rotation_activate": ("k8s-rollout:es-prd/deployment/a,es-prd/statefulset/b,"
                              "manual:tell the operator")}),
    "eso/prd/trello/prd/trello": (["api-key", "bearer-token", "token"], {
        "rotation_mechanism": "manual", "key_bearer-token": "random",
        "rotation_interval": "never", "interval_bearer-token": "14d",
        "notes": "api-key and token cannot be rotated", "rotation_activate": "auto"}),
    "eso/prd/kc/prd/catalog": (["client-id", "client-secret", "jenkins-user"], {
        "rotation_mechanism": "manual",
        "key_client-id": "copy:eso/prd/app/prd/oidc#client_id",
        "key_client-secret": "copy:eso/prd/app/prd/oidc#client_secret",
        "key_jenkins-user": "none", "rotation_activate": "auto"}),
    "eso/prd/yt/prd/webhook": (["token"], {
        "rotation_mechanism": "random", "rotation_interval": "14d",
        "rotation_activate": "jenkins-job:YouTrackConfiguration?ROTATE_TOKEN=true"}),
    "iac/copy": (["token"], {
        "rotation_mechanism": "copy:eso/prd/app/prd/token#token", "rotation_activate": "none"}),
    "jenkins/youtrack": (["admin-token", "webhook-token"], {
        "rotation_mechanism": "youtrack-token",
        "key_webhook-token": "copy:eso/prd/yt/prd/webhook#token",
        "rotation_interval": "14d", "rotation_activate": "none",
        "rotation_expires_at": "2027-01-31"}),
    "shared/wifi": (["password"], {
        "rotation_mechanism": "manual", "rotation_interval": "never",
        "notes": "PSK in every device", "rotation_activate": "none"}),
}

# Keys the contract leaves to others; the check ignores them and the apply keeps them.
FOREIGN = {"rotation": "coordinated", "rotated_at": "2026-01-01", "rotator_status": "ok"}

# What the store holds before the apply: the sweep's annotations, on every leaf.
BEFORE = {
    "eso/prd/app/prd/oidc": {"rotation": "coordinated", "rotation_mechanism": "keycloak",
                             "notes": "Transcript-migrated."},
    "eso/prd/es/prd/creds": {"rotation": "coordinated", "rotation_mechanism": "elasticsearch"},
    "shared/wifi": {"rotation": "coordinated", "rotation_mechanism": "wifi",
                    "rotated_at": "2026-01-01"},
}


def data_of(path):
    return {key: f"SECRET-{path}-{key}" for key in COMPLIANT[path][0]}


def compliant_store():
    return {path: an.Leaf(path, set(keys), dict(meta))
            for path, (keys, meta) in COMPLIANT.items()}


def found(store):
    return [(f.leaf, f.key) for f in an.check(store)]


def messages(store):
    return [str(f) for f in an.check(store)]


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status


class FakeOpenBao:
    """The kv mount over HTTP: per leaf its data (None: current version deleted) and metadata."""

    def __init__(self, leaves):
        self.leaves = copy.deepcopy(leaves)  # path -> {"data": dict | None, "meta": dict}
        self.requests = []  # (method, path, body, content type)
        self.refuse = {}  # leaf -> HTTP status its PATCH answers

    def __call__(self, req):
        url = urllib.parse.urlsplit(req.full_url)
        assert url.netloc == "secrets", url
        path = urllib.parse.unquote(url.path)[len("/v1/"):]
        method = req.get_method()
        body = json.loads(req.data) if req.data else None
        self.requests.append((method, path, body, req.get_header("Content-type")))
        mount, area, leaf = path.split("/", 2)
        assert mount == "kv" and area in ("metadata", "data"), path
        if method == "LIST" and area == "metadata":
            return self.list(leaf)
        if method == "GET" and area == "metadata":
            if leaf not in self.leaves:
                return self.answer(404, {"errors": []})
            return self.answer(200, {"data": {
                "custom_metadata": self.leaves[leaf]["meta"] or None, "current_version": 1}})
        if method == "GET" and area == "data":
            data = self.leaves.get(leaf, {}).get("data")
            if data is None:
                return self.answer(404, {"data": {"data": None, "metadata": {}}})
            return self.answer(200, {"data": {"data": data, "metadata": {"version": 1}}})
        if method == "PATCH" and area == "metadata":
            if req.get_header("Content-type") != "application/merge-patch+json":
                return self.answer(415, {"errors": ["unsupported content type"]})
            if leaf in self.refuse:
                errors = ["1 error occurred:\n\t* permission denied\n\n"]
                return self.answer(self.refuse[leaf], {"errors": errors})
            meta = self.leaves[leaf]["meta"]
            for key, value in body["custom_metadata"].items():
                if value is None:
                    meta.pop(key, None)
                else:
                    meta[key] = value
            return self.answer(204, None)
        return self.answer(405, {"errors": [f"{method} {path} is not served here"]})

    def list(self, prefix):
        names = set()
        for leaf in self.leaves:
            if leaf.startswith(prefix):
                head, sep, _ = leaf[len(prefix):].partition("/")
                names.add(head + sep)
        if not names:
            return self.answer(404, {"errors": []})
        return self.answer(200, {"data": {"keys": sorted(names)}})

    @staticmethod
    def answer(status, doc):
        body = b"" if doc is None else json.dumps(doc).encode()
        if status >= 400:
            raise urllib.error.HTTPError("https://secrets", status, "err", {}, io.BytesIO(body))
        return FakeResponse(status, body)

    def writes(self):
        return [r for r in self.requests if r[0] not in ("GET", "LIST")]

    def meta(self, leaf):
        return self.leaves[leaf]["meta"]


def annotated_bao():
    return FakeOpenBao({path: {"data": data_of(path), "meta": {**meta, **FOREIGN}}
                        for path, (_, meta) in COMPLIANT.items()})


def unannotated_bao():
    return FakeOpenBao({path: {"data": data_of(path), "meta": dict(BEFORE.get(path, {}))}
                        for path in COMPLIANT})


class Run:
    """main() against a fake OpenBao, with a seed written to a temporary directory."""

    def __init__(self, test, bao=None):
        self.bao = bao
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.seed = self.dir / "seed.yaml"
        self.write_seed({path: dict(meta) for path, (_, meta) in COMPLIANT.items()})
        env = mock.patch.dict(os.environ, {"BAO_TOKEN": "tok", "BAO_ADDR": "https://secrets"})
        env.start()
        test.addCleanup(env.stop)

    def write_seed(self, seed):
        self.seed.write_text(yaml.safe_dump(seed, sort_keys=False))

    def __call__(self, *argv):
        self.lines = []
        code = an.main([*argv], opener=self.bao, out=self.lines.append)
        self.text = "\n".join(self.lines)
        return code

    def apply(self, *extra):
        return self(f"--seed={self.seed}", *extra)


class Check(unittest.TestCase):
    """Each departure from design §4 is a finding that names leaf and key."""

    def test_the_compliant_store_has_no_finding(self):
        self.assertEqual(found(compliant_store()), [])

    def test_keys_the_contract_leaves_to_others_are_ignored(self):
        store = compliant_store()
        for leaf in store.values():
            leaf.meta.update(FOREIGN)
        self.assertEqual(found(store), [])

    def test_a_missing_mechanism_or_activate_is_one_finding_each(self):
        for key in ("rotation_mechanism", "rotation_activate"):
            store = compliant_store()
            del store["eso/prd/app/prd/oidc"].meta[key]
            self.assertEqual(found(store), [("eso/prd/app/prd/oidc", key)], key)

    def test_a_missing_interval_is_a_finding_where_a_key_is_neither_copy_nor_none(self):
        store = compliant_store()
        del store["eso/prd/bot/prd/config"].meta["rotation_interval"]
        self.assertEqual(found(store), [("eso/prd/bot/prd/config", "rotation_interval")])

    def test_a_leaf_of_copies_and_none_keys_needs_no_interval(self):
        store = compliant_store()
        self.assertNotIn("rotation_interval", store["eso/prd/kc/prd/catalog"].meta)
        self.assertNotIn("rotation_interval", store["iac/copy"].meta)
        self.assertEqual(found(store), [])

    def test_an_unknown_kind_in_the_mechanism_or_an_override(self):
        store = compliant_store()
        store["eso/prd/app/prd/oidc"].meta["rotation_mechanism"] = "keycloak"
        store["eso/prd/bot/prd/config"].meta["key_telegram-bot-token"] = "telegram"
        self.assertEqual(found(store), [("eso/prd/app/prd/oidc", "rotation_mechanism"),
                                        ("eso/prd/bot/prd/config", "key_telegram-bot-token")])
        self.assertIn("unknown kind 'keycloak'", messages(store)[0])

    def test_a_key_its_kind_does_not_own_and_no_override_names(self):
        store = compliant_store()
        store["eso/prd/app/prd/oidc"].keys.add("url")
        del store["eso/prd/es/prd/creds"].meta["key_username"]
        self.assertEqual(found(store), [("eso/prd/app/prd/oidc", "url"),
                                        ("eso/prd/es/prd/creds", "username")])

    def test_a_one_key_kind_owns_no_key_when_two_are_left(self):
        store = compliant_store()
        del store["eso/prd/bot/prd/config"].meta["key_telegram-chat-id"]
        self.assertEqual(found(store), [("eso/prd/bot/prd/config", "jenkins-token"),
                                        ("eso/prd/bot/prd/config", "telegram-chat-id")])

    def test_the_kind_of_every_key_is_resolved_as_design_3_1_says(self):
        store = compliant_store()
        self.assertEqual(an.resolve(store["eso/prd/app/prd/oidc"]),
                         {"client_id": "none", "client_secret": "keycloak-client"})
        self.assertEqual(an.resolve(store["eso/prd/bot/prd/config"]), {
            "jenkins-token": "jenkins-token", "telegram-bot-token": "manual",
            "telegram-chat-id": "none"})
        self.assertEqual(an.resolve(store["iac/copy"]),
                         {"token": "copy:eso/prd/app/prd/token#token"})
        self.assertEqual(an.resolve(store["eso/prd/trello/prd/trello"]), {
            "api-key": "manual", "bearer-token": "random", "token": "manual"})

    def test_a_stale_override(self):
        store = compliant_store()
        store["shared/wifi"].meta["key_public"] = "none"
        self.assertEqual(found(store), [("shared/wifi", "key_public")])
        self.assertIn("stale override", messages(store)[0])

    def test_a_copy_whose_primary_leaf_or_key_is_missing(self):
        store = compliant_store()
        catalog = store["eso/prd/kc/prd/catalog"].meta
        catalog["key_client-id"] = "copy:eso/prd/gone/prd/oidc#client_id"
        catalog["key_client-secret"] = "copy:eso/prd/app/prd/oidc#secret"
        store["iac/copy"].meta["rotation_mechanism"] = "copy:eso/prd/app/prd/token#gone"
        self.assertEqual(found(store), [("eso/prd/kc/prd/catalog", "key_client-id"),
                                        ("eso/prd/kc/prd/catalog", "key_client-secret"),
                                        ("iac/copy", "rotation_mechanism")])

    def test_a_copy_of_a_none_key_is_valid(self):
        store = compliant_store()
        self.assertEqual(an.resolve(store["eso/prd/app/prd/oidc"])["client_id"], "none")
        self.assertEqual(store["eso/prd/kc/prd/catalog"].meta["key_client-id"],
                         "copy:eso/prd/app/prd/oidc#client_id")
        self.assertEqual(found(store), [])

    def test_a_malformed_interval_or_never_without_notes(self):
        for value in ("14", "2w", "0d", "14 d", "Never"):
            store = compliant_store()
            store["eso/prd/app/prd/token"].meta["rotation_interval"] = value
            self.assertEqual(found(store), [("eso/prd/app/prd/token", "rotation_interval")],
                             value)
        store = compliant_store()
        store["eso/prd/app/prd/token"].meta["rotation_interval"] = "never"
        self.assertEqual(messages(store),
                         ["eso/prd/app/prd/token: rotation_interval: never without notes"])
        store["eso/prd/app/prd/token"].meta["notes"] = "  "
        self.assertEqual(found(store), [("eso/prd/app/prd/token", "rotation_interval")])
        store["eso/prd/app/prd/token"].meta["notes"] = "why it never rotates"
        self.assertEqual(found(store), [])

    def test_a_per_key_interval_is_validated(self):
        cases = {
            "interval_bearer-token": ("3w", "is not <n>d or never"),
            "interval_api-key": ("never", None),
            "interval_gone": ("14d", "the leaf has no key 'gone'"),
        }
        for key, (value, message) in cases.items():
            store = compliant_store()
            store["eso/prd/trello/prd/trello"].meta[key] = value
            if message is None:
                self.assertEqual(found(store), [], key)
            else:
                self.assertEqual(found(store), [("eso/prd/trello/prd/trello", key)], key)
                self.assertIn(message, messages(store)[0])

    def test_a_per_key_never_needs_the_leafs_notes(self):
        store = compliant_store()
        store["eso/prd/bot/prd/config"].meta["interval_telegram-bot-token"] = "never"
        self.assertEqual(messages(store), [("eso/prd/bot/prd/config: interval_telegram-bot-"
                                            "token: never without the leaf's notes")])

    def test_a_per_key_interval_on_a_copy_or_none_key(self):
        store = compliant_store()
        store["eso/prd/kc/prd/catalog"].meta["interval_client-secret"] = "14d"
        store["eso/prd/bot/prd/config"].meta["interval_telegram-chat-id"] = "365d"
        store["eso/prd/app/prd/oidc"].meta["interval_client_id"] = "14d"
        self.assertEqual(found(store), [
            ("eso/prd/app/prd/oidc", "interval_client_id"),
            ("eso/prd/bot/prd/config", "interval_telegram-chat-id"),
            ("eso/prd/kc/prd/catalog", "interval_client-secret")])
        self.assertIn("is none, which takes no interval", messages(store)[0])

    def test_the_activate_forms(self):
        valid = ["auto", "none", "eso", "k8s-rollout", "eso,k8s-rollout,manual:say so",
                 "k8s-rollout:ns/deployment/a", "k8s-rollout:ns/deployment/a,ns/daemonset/b",
                 "jenkins-credential:724520d1", "jenkins-job:IaC/Apply",
                 "jenkins-job:YouTrackConfiguration?ROTATE_TOKEN=true&X=1",
                 "github-webhook:pvginkel/Fieldnotes/123", "argocd-sync:iot-prd",
                 "manual:re-encrypt ca.json then roll step-ca"]
        invalid = ["restart", "auto,manual:say so", "none,eso", "eso:x", "k8s-rollout:a/b",
                   "k8s-rollout,ns/deployment/a", "eso,ns/deployment/a",
                   "k8s-rollout:ns/pod/a", "jenkins-credential:", "github-webhook:repo",
                   "argocd-sync:", "manual:", "manual: ", "", "eso, manual:x"]
        for value in valid:
            store = compliant_store()
            store["eso/prd/app/prd/token"].meta["rotation_activate"] = value
            self.assertEqual(found(store), [], value)
        for value in invalid:
            store = compliant_store()
            store["eso/prd/app/prd/token"].meta["rotation_activate"] = value
            self.assertIn(("eso/prd/app/prd/token", "rotation_activate"), found(store), value)

    def test_rotation_args_that_are_not_json_or_too_large(self):
        for value in ("{realm: homelab}", json.dumps({"x": "y" * 520})):
            store = compliant_store()
            store["eso/prd/app/prd/oidc"].meta["rotation_args"] = value
            self.assertEqual(found(store), [("eso/prd/app/prd/oidc", "rotation_args")], value)

    def test_rotation_expires_at_that_is_not_an_iso_date(self):
        for value in ("2027-13-01", "20270131", "soon", "2027-01-31T00:00:00"):
            store = compliant_store()
            store["jenkins/youtrack"].meta["rotation_expires_at"] = value
            self.assertEqual(found(store), [("jenkins/youtrack", "rotation_expires_at")], value)

    def test_a_leaf_whose_keys_cannot_be_read_is_a_finding_and_its_metadata_still_checked(self):
        store = compliant_store()
        store["shared/wifi"].keys = None
        store["shared/wifi"].meta["rotation_interval"] = "2w"
        self.assertEqual(found(store), [("shared/wifi", "(data)"),
                                        ("shared/wifi", "rotation_interval")])

    def test_an_unannotated_leaf_is_reported_once_per_missing_key(self):
        store = compliant_store()
        store["eso/prd/bot/prd/config"].meta = {"rotation": "coordinated"}
        self.assertEqual(found(store), [("eso/prd/bot/prd/config", "rotation_mechanism"),
                                        ("eso/prd/bot/prd/config", "rotation_activate"),
                                        ("eso/prd/bot/prd/config", "rotation_interval")])


class LiveCheck(unittest.TestCase):
    """--check over OpenBao: reads, writes nothing, prints no value."""

    def test_a_compliant_store_exits_0(self):
        run = Run(self, annotated_bao())
        self.assertEqual(run("--check"), 0, run.text)
        self.assertEqual(run.lines, ["0 finding(s) on 0 of 10 leaf(s)"])

    def test_findings_exit_1_and_print_no_value(self):
        run = Run(self, unannotated_bao())
        self.assertEqual(run("--check"), 1)
        self.assertTrue(any(line.startswith("shared/wifi: rotation_mechanism: unknown kind "
                                            "'wifi'") for line in run.lines), run.text)
        self.assertNotIn("SECRET", run.text)

    def test_the_check_writes_nothing(self):
        run = Run(self, unannotated_bao())
        run("--check")
        self.assertEqual(run.bao.writes(), [])

    def test_a_deleted_current_version_is_a_finding(self):
        bao = annotated_bao()
        bao.leaves["shared/wifi"]["data"] = None
        run = Run(self, bao)
        self.assertEqual(run("--check"), 1)
        self.assertEqual(run.lines[0], "shared/wifi: (data): its current version is deleted or "
                                       "destroyed: its keys cannot be read")


class Apply(unittest.TestCase):
    """The seed written with PATCH kv/metadata only, idempotent, stopped by a refusal."""

    def test_a_dry_run_lists_each_change_and_writes_nothing(self):
        run = Run(self, unannotated_bao())
        self.assertEqual(run.apply(), 0, run.text)
        self.assertEqual(run.bao.writes(), [])
        self.assertIn("eso/prd/app/prd/oidc", run.lines)
        self.assertIn("  add     rotation_interval=14d", run.lines)
        self.assertIn("  change  rotation_mechanism=keycloak-client  (was keycloak)", run.lines)
        self.assertEqual(run.lines[-1], "would patch (dry run; --apply writes) 10 leaf(s); "
                                        "0 unchanged, 0 absent from the store, 0 live leaf(s) "
                                        "not in the seed")

    def test_the_apply_reads_metadata_only(self):
        run = Run(self, unannotated_bao())
        run.apply("--apply")
        self.assertEqual({(m, p.split("/")[1]) for m, p, _, _ in run.bao.requests},
                         {("LIST", "metadata"), ("GET", "metadata"), ("PATCH", "metadata")})

    def test_every_write_is_a_merge_patch_of_the_changed_keys(self):
        run = Run(self, unannotated_bao())
        self.assertEqual(run.apply("--apply"), 0, run.text)
        writes = run.bao.writes()
        self.assertEqual(len(writes), 10)
        for method, path, body, ctype in writes:
            self.assertEqual((method, ctype), ("PATCH", "application/merge-patch+json"))
            self.assertTrue(path.startswith("kv/metadata/"), path)
            self.assertEqual(list(body), ["custom_metadata"])
        wifi = next(b for _, p, b, _ in writes if p == "kv/metadata/shared/wifi")
        self.assertEqual(wifi["custom_metadata"], {
            "rotation_mechanism": "manual", "rotation_interval": "never",
            "notes": "PSK in every device", "rotation_activate": "none"})

    def test_keys_the_seed_does_not_name_survive(self):
        run = Run(self, unannotated_bao())
        run.apply("--apply")
        self.assertEqual(run.bao.meta("shared/wifi")["rotated_at"], "2026-01-01")
        self.assertEqual(run.bao.meta("eso/prd/app/prd/oidc")["rotation"], "coordinated")

    def test_after_the_apply_the_check_passes(self):
        run = Run(self, unannotated_bao())
        run.apply("--apply")
        self.assertEqual(run("--check"), 0, run.text)

    def test_a_second_apply_changes_nothing(self):
        run = Run(self, unannotated_bao())
        run.apply("--apply")
        before = len(run.bao.writes())
        self.assertEqual(run.apply("--apply"), 0)
        self.assertEqual(len(run.bao.writes()), before)
        self.assertEqual(run.lines[-1], "patching 0 leaf(s); 10 unchanged, 0 absent from the "
                                        "store, 0 live leaf(s) not in the seed")

    def test_seed_notes_keep_the_earlier_notes(self):
        run = Run(self, unannotated_bao())
        seed = {path: dict(meta) for path, (_, meta) in COMPLIANT.items()}
        seed["eso/prd/app/prd/oidc"]["notes"] = "the client of app"
        run.write_seed(seed)
        run.apply("--apply")
        self.assertEqual(run.bao.meta("eso/prd/app/prd/oidc")["notes"],
                         "the client of app | earlier: Transcript-migrated.")
        before = len(run.bao.writes())
        run.apply("--apply")
        self.assertEqual(len(run.bao.writes()), before)

    def test_a_seed_leaf_the_store_lacks_is_reported_and_skipped(self):
        bao = unannotated_bao()
        del bao.leaves["shared/wifi"]
        run = Run(self, bao)
        self.assertEqual(run.apply("--apply"), 0, run.text)
        self.assertIn("absent from the store, skipped: shared/wifi", run.lines)
        self.assertNotIn("kv/metadata/shared/wifi", [p for _, p, _, _ in bao.writes()])

    def test_a_live_leaf_the_seed_lacks_is_reported(self):
        bao = unannotated_bao()
        bao.leaves["eso/prd/new/prd/thing"] = {"data": {"k": "SECRET-x"}, "meta": {}}
        run = Run(self, bao)
        self.assertEqual(run.apply(), 0)
        self.assertIn("not in the seed: eso/prd/new/prd/thing", run.lines)

    def test_a_refused_write_stops_the_run_and_names_the_patch_capability(self):
        bao = unannotated_bao()
        bao.refuse["eso/prd/app/prd/token"] = 403
        run = Run(self, bao)
        self.assertEqual(run.apply("--apply"), 1)
        self.assertEqual([p for _, p, _, _ in bao.writes()],
                         ["kv/metadata/eso/prd/app/prd/oidc", "kv/metadata/eso/prd/app/prd/token"])
        last = run.lines[-1]
        self.assertTrue(last.startswith("stopped at eso/prd/app/prd/token: OpenBao refused"), last)
        self.assertIn("lacks the patch capability on the kv mount", last)
        self.assertIn("site-openbao.yml converge grants it", last)
        self.assertIn("Patched 1 of 10 leaf(s); run the apply again after the converge", last)

    def test_a_refusal_at_the_first_write_writes_nothing_more(self):
        bao = unannotated_bao()
        bao.refuse = {path: 403 for path in COMPLIANT}
        run = Run(self, bao)
        self.assertEqual(run.apply("--apply"), 1)
        self.assertEqual(len(bao.writes()), 1)
        self.assertIn("Patched 0 of 10", run.lines[-1])

    def test_any_other_failed_write_stops_the_run_too(self):
        bao = unannotated_bao()
        bao.refuse["eso/prd/app/prd/oidc"] = 500
        run = Run(self, bao)
        self.assertEqual(run.apply("--apply"), 1)
        self.assertEqual(len(bao.writes()), 1)
        self.assertIn("HTTP 500", run.lines[-1])
        self.assertNotIn("patch capability", run.lines[-1])

    def test_a_merged_notes_too_long_writes_nothing(self):
        bao = unannotated_bao()
        bao.leaves["eso/prd/app/prd/oidc"]["meta"]["notes"] = "x" * 500
        run = Run(self, bao)
        seed = {path: dict(meta) for path, (_, meta) in COMPLIANT.items()}
        seed["eso/prd/app/prd/oidc"]["notes"] = "the client of app"
        run.write_seed(seed)
        self.assertEqual(run.apply("--apply"), 1)
        self.assertEqual(bao.writes(), [])
        self.assertIn("cannot write: eso/prd/app/prd/oidc: notes: with the earlier notes kept, "
                      "longer than 512 bytes", run.lines)

    def test_a_seed_problem_reaches_openbao_not_at_all(self):
        run = Run(self, unannotated_bao())
        seed = {path: dict(meta) for path, (_, meta) in COMPLIANT.items()}
        seed["shared/wifi"]["rotated_at"] = "2026-10-04"
        run.write_seed(seed)
        self.assertEqual(run.apply("--apply"), 1)
        self.assertEqual(run.bao.requests, [])
        self.assertIn("shared/wifi: rotated_at: not an operator key of the contract", run.text)


class Seed(unittest.TestCase):
    """What a seed may hold: operator keys of design §4, strings, within the metadata limits."""

    def load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seed.yaml"
            path.write_text(text)
            return an.load_seed(path)

    def problems(self, text):
        with self.assertRaises(an.SeedError) as e:
            self.load(text)
        return str(e.exception)

    def test_a_seed_maps_leaf_paths_to_string_metadata(self):
        self.assertEqual(self.load("iac/x:\n  rotation_mechanism: manual\n  key_a-b_c: none\n"
                                   "  interval_a-b_c: never\n"),
                         {"iac/x": {"rotation_mechanism": "manual", "key_a-b_c": "none",
                                    "interval_a-b_c": "never"}})

    def test_keys_that_are_not_the_operators(self):
        text = self.problems("iac/x:\n  rotated_at: '2026-10-04'\n  rotator_status: ok\n"
                             "  rotation: coordinated\n")
        for key in ("rotated_at", "rotator_status", "rotation"):
            self.assertIn(f"iac/x: {key}: not an operator key of the contract", text)

    def test_values_that_are_not_strings_or_too_long(self):
        text = self.problems(f"iac/x:\n  rotation_interval: 14\n  notes: {'n' * 513}\n")
        self.assertIn("iac/x: rotation_interval: not a string", text)
        self.assertIn("iac/x: notes: longer than 512 bytes", text)

    def test_too_many_keys_for_one_leaf(self):
        keys = "".join(f"  key_k{i}: none\n" for i in range(65))
        self.assertIn("iac/x: 65 metadata keys, more than 64", self.problems(f"iac/x:\n{keys}"))

    def test_a_leaf_or_key_given_twice(self):
        self.assertIn("iac/x appears twice", self.problems(
            "iac/x:\n  notes: a\niac/x:\n  notes: b\n"))
        self.assertIn("notes appears twice", self.problems("iac/x:\n  notes: a\n  notes: b\n"))

    def test_paths_that_are_not_leaves(self):
        for path in ("/iac/x", "iac/x/", "iac//x"):
            self.assertIn("not a leaf path", self.problems(f"'{path}':\n  notes: a\n"), path)

    def test_a_seed_that_is_not_a_mapping(self):
        self.assertIn("not a mapping of leaf paths", self.problems("- iac/x\n"))
        self.assertIn("iac/x: not a mapping of metadata keys", self.problems("iac/x: manual\n"))


class Offline(unittest.TestCase):
    """--check --keys: the seed over the key names of a file, without OpenBao."""

    def setUp(self):
        def no_network(req):
            raise AssertionError(f"the offline check called {req.full_url}")
        self.run_ = Run(self, no_network)
        self.keys = self.run_.dir / "keys.json"
        self.names = {path: keys for path, (keys, _) in COMPLIANT.items()}

    def check(self):
        self.keys.write_text(json.dumps(self.names))
        return self.run_("--check", f"--keys={self.keys}", f"--seed={self.run_.seed}")

    def test_the_seed_resolves_every_key(self):
        self.assertEqual(self.check(), 0, self.run_.text)

    def test_it_needs_no_token(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.check(), 0, self.run_.text)

    def test_a_leaf_the_seed_does_not_cover_is_a_finding(self):
        self.names["eso/prd/new/prd/thing"] = ["token"]
        self.assertEqual(self.check(), 1)
        self.assertIn("eso/prd/new/prd/thing: rotation_mechanism: missing", self.run_.lines)

    def test_a_seed_leaf_missing_from_the_key_file_is_reported_not_failed(self):
        del self.names["shared/wifi"]
        self.assertEqual(self.check(), 0, self.run_.text)
        self.assertIn("seed leaf not in the key file: shared/wifi", self.run_.lines)

    def test_a_key_the_seed_does_not_resolve_is_a_finding(self):
        self.names["eso/prd/app/prd/oidc"] = ["client_id", "client_secret", "url"]
        self.assertEqual(self.check(), 1)
        self.assertIn("eso/prd/app/prd/oidc: url: no kind resolves it: keycloak-client does "
                      "not own it and no key_url names one", self.run_.lines)

    def test_a_key_file_that_is_not_leaf_to_key_names(self):
        self.names = {"iac/x": "token"}
        self.assertEqual(self.check(), 1)
        self.assertIn("not a JSON object of leaf path -> key names", self.run_.text)


class BaoClient(unittest.TestCase):
    def test_the_walk_lists_every_leaf_of_the_mount(self):
        bao = an.Bao("https://secrets", "tok", opener=annotated_bao())
        self.assertEqual(bao.leaves(), sorted(COMPLIANT))

    def test_keys_are_names_only_and_none_for_a_deleted_version(self):
        fake = annotated_bao()
        fake.leaves["shared/wifi"]["data"] = None
        bao = an.Bao("https://secrets", "tok", opener=fake)
        self.assertEqual(bao.keys("eso/prd/app/prd/oidc"), {"client_id", "client_secret"})
        self.assertIsNone(bao.keys("shared/wifi"))
        self.assertIsNone(bao.keys("no/such/leaf"))

    def test_patch_sends_a_merge_patch_of_custom_metadata(self):
        fake = annotated_bao()
        an.Bao("https://secrets", "tok", opener=fake).patch("shared/wifi", {"notes": "n"})
        self.assertEqual(fake.requests, [("PATCH", "kv/metadata/shared/wifi",
                                          {"custom_metadata": {"notes": "n"}},
                                          "application/merge-patch+json")])

    def test_a_refusal_carries_its_status(self):
        fake = annotated_bao()
        fake.refuse["shared/wifi"] = 403
        with self.assertRaises(an.BaoError) as e:
            an.Bao("https://secrets", "tok", opener=fake).patch("shared/wifi", {"notes": "n"})
        self.assertEqual(e.exception.status, 403)
        self.assertIn("HTTP 403", str(e.exception))


class Main(unittest.TestCase):
    def usage(self, *argv, env=None):
        env = {"BAO_TOKEN": "tok"} if env is None else env
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("sys.stderr", io.StringIO()) as err, \
                self.assertRaises(SystemExit) as e:
            an.main(list(argv), opener=lambda req: self.fail("no request expected"),
                    out=lambda line: None)
        return e.exception.code, err.getvalue()

    def test_check_and_apply_exclude_each_other(self):
        self.assertEqual(self.usage("--check", "--apply")[0], 2)

    def test_keys_needs_check(self):
        self.assertEqual(self.usage("--keys=k.json")[0], 2)

    def test_the_live_check_takes_no_seed(self):
        self.assertEqual(self.usage("--check", "--seed=s.yaml")[0], 2)

    def test_a_missing_token_is_a_usage_error(self):
        code, err = self.usage("--check", env={})
        self.assertEqual(code, 2)
        self.assertIn("bao-login.sh", err)

    def test_a_missing_seed_is_an_error_not_a_trace(self):
        run = Run(self, unannotated_bao())
        self.assertEqual(run(f"--seed={run.dir / 'none.yaml'}"), 1)
        self.assertTrue(run.lines[-1].startswith("error: "), run.lines)


if __name__ == "__main__":
    unittest.main()
