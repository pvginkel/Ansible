"""accessor_cleanup: what it proves, what it destroys, and that no secret_id leaves the process.

Run: python3 -m unittest discover -s scripts/rotation
"""

import base64
import builtins
import io
import json
import os
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import accessor_cleanup as ac

ROLE_IDS = {r: f"roleid-{r}" for r in ac.ROLES}


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def ansible_out(results: dict) -> str:
    return json.dumps({"plays": [{"tasks": [{"hosts": results}]}],
                       "stats": {host: {} for host in results}})


class FakeBao:
    """The AppRole store: per role, its live secret_ids and their accessors."""

    def __init__(self, events: list):
        self.events = events
        self.live = {r: {} for r in ac.ROLES}  # role -> {secret_id: accessor}
        self.fail = set()  # (method name, role) pairs that raise BaoError
        self.minted = 0

    def add(self, role, secret_id, accessor):
        self.live[role][secret_id] = accessor

    def _check(self, name, role):
        if (name, role) in self.fail:
            raise ac.BaoError(f"{name} {role}: HTTP 500")

    def role_id(self, role):
        self._check("role_id", role)
        return ROLE_IDS[role]

    def accessors(self, role):
        self._check("accessors", role)
        return list(self.live[role].values())

    def created(self, role, accessor):
        return f"created-{accessor}"

    def accessor_of(self, role, secret_id):
        self._check("accessor_of", role)
        return self.live[role].get(secret_id)

    def destroy(self, role, accessor):
        self._check("destroy", role)
        self.events.append(("destroy", role, accessor))
        self.live[role] = {s: a for s, a in self.live[role].items() if a != accessor}

    def mint(self, role):
        self.minted += 1
        secret_id, accessor = f"SECRET-{role}-fresh", f"acc-{role}-fresh"
        self.add(role, secret_id, accessor)
        self.events.append(("mint", role, accessor))
        return secret_id, accessor


class FakeJenkins:
    def __init__(self, creds=None, body=None, error=None):
        self.creds, self.body, self.error = creds or [], body, error

    def run(self, groovy):
        if self.error:
            raise self.error
        if self.body is not None:
            return self.body
        return ac.JENKINS_MARKER + json.dumps(self.creds) + "\n"


class FakeTerminal:
    def __init__(self, events: list, answers: list[str]):
        self.events, self.answers = events, list(answers)
        self.revealed = []

    def ask(self, prompt):
        answer = self.answers.pop(0)
        self.events.append(("ask", answer))
        return answer

    def reveal(self, secret, prompt):
        self.revealed.append(secret)
        self.events.append(("reveal",))


class World:
    """Every consumer, as the commands the script runs would find them."""

    def __init__(self):
        self.events = []
        self.argvs = []
        self.bao = FakeBao(self.events)
        self.admin_msg = "SECRET-admin-held"
        self.iac_yaml = ("env:\n"
                         "  - name: OPENBAO_URL\n    value: https://secrets.home/\n"
                         "  - name: OPENBAO_ROLE_ID\n    value: rid\n"
                         "  - name: OPENBAO_SECRET_ID\n    value: SECRET-iac-held\n"
                         "  - name: OTHER\n    value: !bao kv/iac/x#y\n")
        self.backup = {h: "SECRET-backup-held\n" for h in ("srvvault1", "srvvault2", "srvvault3")}
        self.unreachable = set()
        self.clusters = {"prd": "SECRET-eso-held", "dev": "SECRET-esodev-held"}
        self.cluster_role_ids = {"prd": ROLE_IDS["eso"], "dev": ROLE_IDS["eso-dev"]}
        self.cluster_down = set()
        self.jenkins = FakeJenkins([
            {"store": "Jenkins", "id": "cred-1", "roleId": ROLE_IDS["jenkins"],
             "secretId": "SECRET-jenkins-held"},
            {"store": "Jenkins", "id": "cred-other", "roleId": "roleid-elsewhere",
             "secretId": "SECRET-elsewhere"},
        ])
        self.terminal = FakeTerminal(self.events, [])
        for role, held in (("openbao-admin", "SECRET-admin-held"), ("iac-agent", "SECRET-iac-held"),
                           ("jenkins", "SECRET-jenkins-held"), ("eso", "SECRET-eso-held"),
                           ("eso-dev", "SECRET-esodev-held"), ("backup", "SECRET-backup-held")):
            self.bao.add(role, held, f"acc-{role}-held")
        for role, n in (("openbao-admin", 3), ("iac-agent", 3), ("jenkins", 4), ("eso", 4),
                        ("eso-dev", 1)):
            for i in range(n):
                self.bao.add(role, f"SECRET-{role}-stale-{i}", f"acc-{role}-stale-{i}")

    def secrets(self):
        """Every secret_id anywhere in this world."""
        return [s for live in self.bao.live.values() for s in live] + ["SECRET-elsewhere"]

    def run(self, argv, cwd=None):
        self.argvs.append(list(argv))
        if "ansible" in argv:
            i = argv.index("ansible")
            return self.ansible(argv[i + 1], argv[i + 3], argv[i + 5], cwd)
        if "kubectl" in argv:
            return self.kubectl(argv)
        raise AssertionError(f"unexpected command {argv}")

    def ansible(self, pattern, module, args, cwd):
        assert cwd == Path("/repo/ansible"), cwd
        if pattern == "srvvault1" and module == "debug":
            assert args == "msg={{ openbao_admin_secret_id }}"
            return ac.Completed(0, ansible_out({"srvvault1": {"msg": self.admin_msg}}), "")
        if pattern == "srviac" and module == "slurp":
            assert args == "src=/etc/iac/secrets.yaml"
            return ac.Completed(0, ansible_out({"srviac": {"content": b64(self.iac_yaml)}}), "")
        if pattern == "openbao" and module == "slurp":
            assert args == "src=/etc/openbao/backup-secret-id"
            results = {h: ({"unreachable": True, "msg": "ssh: no route to host"}
                           if h in self.unreachable else {"content": b64(v)})
                       for h, v in self.backup.items()}
            return ac.Completed(4 if self.unreachable else 0, ansible_out(results), "")
        raise AssertionError((pattern, module, args))

    def kubectl(self, argv):
        cluster = "prd" if "/home/op/.kube/config-prd-write" in argv else "dev"
        assert f"/home/op/.kube/config-{cluster}-write" in argv
        if cluster in self.cluster_down:
            return ac.Completed(1, "", "Unable to connect to the server: no route to host")
        args = argv[argv.index("get") + 1:]
        if args[0].startswith("clustersecretstores"):
            return ac.Completed(0, json.dumps({"items": [
                {"kind": "ClusterSecretStore", "metadata": {"name": f"openbao-{cluster}"},
                 "spec": {"provider": {"vault": {"auth": {"appRole": {
                     "roleRef": {"name": "openbao-eso-approle", "key": "role_id",
                                 "namespace": f"external-secrets-{cluster}"},
                     "secretRef": {"name": "openbao-eso-approle", "key": "secret_id",
                                   "namespace": f"external-secrets-{cluster}"}}}}}}},
                {"kind": "SecretStore", "metadata": {"name": "other", "namespace": "app"},
                 "spec": {"provider": {"vault": {"auth": {"appRole": {
                     "roleId": "roleid-elsewhere",
                     "secretRef": {"name": "x", "key": "secret_id"}}}}}}},
                {"kind": "ClusterSecretStore", "metadata": {"name": "fake"},
                 "spec": {"provider": {"fake": {}}}},
            ]}), "")
        assert args[:3] == ["secret", "--namespace", f"external-secrets-{cluster}"], args
        return ac.Completed(0, json.dumps({"data": {
            "role_id": b64(self.cluster_role_ids[cluster]),
            "secret_id": b64(self.clusters[cluster])}}), "")

    def context(self, fallback=False):
        return ac.Context(bao=self.bao, run=self.run, jenkins=self.jenkins,
                          terminal=self.terminal, jenkins_fallback=fallback,
                          out=lambda line: self.events.append(("out", line)),
                          repo=Path("/repo"), home=Path("/home/op"))

    def execute(self, roles=ac.ROLES, apply=False, fallback=False):
        return ac.execute(self.context(fallback), list(roles), apply)

    def output(self):
        return "\n".join(e[1] for e in self.events if e[0] == "out")

    def destroyed(self, role=None):
        return [e[2] for e in self.events if e[0] == "destroy" and (role is None or e[1] == role)]

    def plan(self, role):
        return ac.plan_role(self.context(), role)


class NoSecretLeaves(unittest.TestCase):
    """No secret_id on stdout, on a command line, or in a file."""

    def assert_no_secret(self, world, text):
        for secret in world.secrets():
            self.assertNotIn(secret, text)

    def test_a_full_apply_prints_and_passes_no_secret_id(self):
        w = World()
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stdout):
            self.assertEqual(w.execute(apply=True), 0)
        self.assert_no_secret(w, w.output() + stdout.getvalue())
        self.assert_no_secret(w, json.dumps(w.argvs))

    def test_a_run_with_every_role_unproven_prints_no_secret_id(self):
        w = World()
        w.admin_msg = "SECRET-not-live"
        w.iac_yaml = "env:\n  - name: OPENBAO_SECRET_ID\n    value: SECRET-iac-held\n  bad: [\n"
        w.unreachable.add("srvvault2")
        w.cluster_down.update({"prd", "dev"})
        w.jenkins.body = "groovy.lang.MissingPropertyException: SECRET-jenkins-held\n"
        self.assertEqual(w.execute(apply=True), 1)
        self.assert_no_secret(w, w.output())
        self.assertNotIn("SECRET-not-live", w.output())
        self.assertEqual(w.destroyed(), [])

    def test_a_full_run_opens_no_file(self):
        w = World()
        with mock.patch.object(builtins, "open", side_effect=AssertionError("open() called")):
            self.assertEqual(w.execute(apply=True), 0)


class Planning(unittest.TestCase):
    def test_every_accessor_but_the_held_ones_is_planned_for_destruction(self):
        w = World()
        p = w.plan("eso")
        self.assertIsNone(p.untouched)
        self.assertEqual(list(p.held), ["acc-eso-held"])
        self.assertEqual(p.destroy, [f"acc-eso-stale-{i}" for i in range(4)])

    def test_consumers_sharing_a_secret_id_keep_one_accessor(self):
        w = World()
        w.bao.add("backup", "SECRET-backup-old", "acc-backup-old")
        p = w.plan("backup")
        self.assertEqual(p.held, {"acc-backup-held": [
            f"srvvault{i}:/etc/openbao/backup-secret-id" for i in (1, 2, 3)]})
        self.assertEqual(p.destroy, ["acc-backup-old"])

    def test_consumers_holding_different_secret_ids_keep_each(self):
        w = World()
        w.bao.add("backup", "SECRET-backup-other", "acc-backup-other")
        w.backup["srvvault3"] = "SECRET-backup-other"
        p = w.plan("backup")
        self.assertEqual(sorted(p.held), ["acc-backup-held", "acc-backup-other"])
        self.assertEqual(p.destroy, [])

    def test_a_role_with_nothing_stale_destroys_nothing(self):
        p = World().plan("backup")
        self.assertIsNone(p.untouched)
        self.assertEqual(p.destroy, [])


class Unproven(unittest.TestCase):
    """A role whose held accessor is not proven keeps every accessor."""

    def assert_untouched(self, w, role, reason):
        self.assertEqual(w.execute(apply=True), 1)
        self.assertEqual(w.destroyed(role), [])
        self.assertIn(f"{role}: untouched: ", w.output())
        self.assertIn(reason, w.output())
        others = [r for r in ac.ROLES if r != role and r != "backup"]
        self.assertTrue(all(w.destroyed(r) for r in others), w.output())

    def test_a_held_secret_id_the_lookup_does_not_find(self):
        w = World()
        w.admin_msg = "SECRET-not-live"
        self.assert_untouched(w, "openbao-admin",
                              "ansible-vault openbao_admin_secret_id holds no live secret_id")

    def test_an_empty_held_secret_id(self):
        w = World()
        w.admin_msg = ""
        self.assert_untouched(w, "openbao-admin", "openbao_admin_secret_id is empty")

    def test_an_unreachable_cluster(self):
        w = World()
        w.cluster_down.add("dev")
        self.assert_untouched(w, "eso-dev", "dev cluster: kubectl get")

    def test_a_cluster_whose_stores_do_not_authenticate_as_the_role(self):
        w = World()
        w.cluster_role_ids["prd"] = "roleid-elsewhere"
        self.assert_untouched(w, "eso", "no ESO store on the prd cluster authenticates as eso")

    def test_one_unreachable_backup_host(self):
        w = World()
        w.bao.add("backup", "SECRET-backup-old", "acc-backup-old")
        w.unreachable.add("srvvault2")
        self.assertEqual(w.execute(apply=True), 1)
        self.assertEqual(w.destroyed("backup"), [])
        self.assertIn("srvvault2: unreachable", w.output())

    def test_a_literal_the_secrets_file_does_not_hold(self):
        w = World()
        w.iac_yaml = "env:\n  - name: OPENBAO_SECRET_ID\n    value: !bao kv/iac/x#y\n"
        self.assert_untouched(w, "iac-agent", "no single literal OPENBAO_SECRET_ID")

    def test_a_secrets_file_that_is_not_yaml_is_reported_without_its_text(self):
        w = World()
        w.iac_yaml = "env:\n  - name: OPENBAO_SECRET_ID\n    value: SECRET-iac-held: x\n"
        self.assert_untouched(w, "iac-agent", "is not valid YAML at line")
        self.assertNotIn("SECRET-iac-held", w.output())

    def test_an_openbao_error_while_listing(self):
        w = World()
        w.bao.fail.add(("accessors", "eso"))
        self.assert_untouched(w, "eso", "accessors eso: HTTP 500")

    def test_an_openbao_error_while_looking_up(self):
        w = World()
        w.bao.fail.add(("accessor_of", "iac-agent"))
        self.assert_untouched(w, "iac-agent", "accessor_of iac-agent: HTTP 500")


class Applying(unittest.TestCase):
    def test_a_dry_run_destroys_nothing(self):
        w = World()
        self.assertEqual(w.execute(), 0)
        self.assertEqual(w.destroyed(), [])
        self.assertIn("would destroy (dry run; --apply destroys) 15 accessor(s); untouched: none",
                      w.output())

    def test_the_whole_plan_is_shown_before_the_first_destroy(self):
        w = World()
        w.execute(apply=True)
        first = next(i for i, e in enumerate(w.events) if e[0] == "destroy")
        shown = [e[1] for e in w.events[:first] if e[0] == "out"]
        for role in ac.ROLES:
            self.assertIn(f"{role}: proven", shown)
        planned = [line.split()[1] for line in shown if line.startswith("  destroy ")]
        self.assertEqual(sorted(planned), sorted(w.destroyed()))
        self.assertEqual(len(planned), 15)

    def test_apply_leaves_each_role_only_its_held_accessors(self):
        w = World()
        self.assertEqual(w.execute(apply=True), 0)
        for role in ac.ROLES:
            self.assertEqual(list(w.bao.live[role].values()), [f"acc-{role}-held"])

    def test_a_failed_destroy_stops_the_run(self):
        w = World()
        w.bao.fail.add(("destroy", "iac-agent"))
        self.assertEqual(w.execute(apply=True), 1)
        self.assertEqual(w.destroyed("jenkins"), [])
        self.assertTrue(w.output().endswith("stopped: destroy iac-agent: HTTP 500; "
                                            "the accessors reported destroyed above are gone"))

    def test_only_the_selected_roles_are_read(self):
        w = World()
        self.assertEqual(w.execute(roles=["eso"], apply=True), 0)
        self.assertEqual(set(e[1] for e in w.events if e[0] == "destroy"), {"eso"})
        self.assertTrue(all("kubectl" in argv for argv in w.argvs))


class JenkinsRead(unittest.TestCase):
    def test_only_credentials_with_the_role_id_are_held(self):
        w = World()
        w.jenkins.creds.append(dict(w.jenkins.creds[0]))  # the same credential twice
        p = w.plan("jenkins")
        self.assertEqual(p.held, {"acc-jenkins-held": ["Jenkins credential cred-1 (Jenkins)"]})
        self.assertEqual(len(p.destroy), 4)

    def test_no_credential_with_the_role_id(self):
        w = World()
        w.jenkins.creds = w.jenkins.creds[1:]
        p = w.plan("jenkins")
        self.assertIn("none of the 1 Vault AppRole credentials in Jenkins carries the jenkins "
                      "role_id", p.untouched)

    def test_a_console_answer_without_the_result_line(self):
        w = World()
        w.jenkins.body = "groovy.lang.MissingPropertyException: No such property\n"
        p = w.plan("jenkins")
        self.assertIn("returned no credential list", p.untouched)
        self.assertIn("--apply --jenkins-fallback", p.untouched)

    def test_no_jenkins_credentials_in_the_environment(self):
        w = World()
        w.jenkins = None
        self.assertIn("JENKINS_URL, JENKINS_USER and JENKINS_TOKEN", w.plan("jenkins").untouched)


class JenkinsFallback(unittest.TestCase):
    def failing_world(self, answers):
        w = World()
        w.jenkins.error = ac.Unproven("the Jenkins script console answered HTTP 403")
        w.terminal.answers = answers
        return w

    def test_without_the_flag_a_failed_read_mints_nothing(self):
        w = self.failing_world([])
        self.assertEqual(w.execute(roles=["jenkins"], apply=True), 1)
        self.assertEqual(w.bao.minted, 0)
        self.assertEqual(w.destroyed(), [])

    def test_a_declined_mint_mints_nothing(self):
        w = self.failing_world(["no"])
        self.assertEqual(w.execute(roles=["jenkins"], apply=True, fallback=True), 1)
        self.assertEqual(w.bao.minted, 0)
        self.assertEqual(w.destroyed(), [])
        self.assertIn("the fallback mint was declined", w.output())

    def test_an_unconfirmed_build_destroys_nothing_for_jenkins(self):
        w = self.failing_world(["mint", "no"])
        self.assertEqual(w.execute(apply=True, fallback=True), 1)
        self.assertEqual(w.destroyed("jenkins"), [])
        self.assertIn("(accessor acc-jenkins-fresh) was not confirmed", w.output())
        self.assertEqual(len(w.destroyed("eso")), 4)

    def test_a_confirmed_build_makes_the_fresh_accessor_the_held_one(self):
        w = self.failing_world(["mint", "passed"])
        self.assertEqual(w.execute(roles=["jenkins"], apply=True, fallback=True), 0)
        self.assertEqual(list(w.bao.live["jenkins"].values()), ["acc-jenkins-fresh"])
        kinds = [e[0] for e in w.events if e[0] in ("mint", "reveal", "ask", "destroy")]
        self.assertEqual(kinds[:4], ["ask", "mint", "reveal", "ask"])
        self.assertEqual(set(kinds[4:]), {"destroy"})
        self.assertEqual(len(kinds[4:]), 5)

    def test_the_fresh_secret_id_reaches_the_terminal_only(self):
        w = self.failing_world(["mint", "passed"])
        w.execute(roles=["jenkins"], apply=True, fallback=True)
        self.assertEqual(w.terminal.revealed, ["SECRET-jenkins-fresh"])
        self.assertNotIn("SECRET-jenkins-fresh", w.output())
        self.assertIn("minted a fresh secret_id, accessor acc-jenkins-fresh", w.output())

    def test_a_working_read_mints_nothing_with_the_flag(self):
        w = World()
        self.assertEqual(w.execute(roles=["jenkins"], apply=True, fallback=True), 0)
        self.assertEqual(w.bao.minted, 0)
        self.assertEqual(w.terminal.revealed, [])

    def test_an_openbao_error_is_no_reason_to_mint(self):
        w = World()
        w.bao.fail.add(("role_id", "jenkins"))
        self.assertEqual(w.execute(roles=["jenkins"], apply=True, fallback=True), 1)
        self.assertEqual(w.bao.minted, 0)


class FakeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status


class BaoClient(unittest.TestCase):
    """The HTTP calls: endpoints, methods, and where the secret_id goes."""

    def client(self, status, doc):
        bao = ac.Bao("https://secrets/", "tok", None)
        self.requests = []
        body = b"" if doc is None else json.dumps(doc).encode()

        def opener(req):
            self.requests.append(req)
            if status >= 400:
                raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(body))
            return FakeResponse(status, body)
        bao.open = opener
        return bao

    def sent(self):
        req = self.requests[-1]
        return req.get_method(), req.full_url, json.loads(req.data) if req.data else None

    def test_destroy_posts_the_accessor_to_secret_id_accessor_destroy(self):
        bao = self.client(204, None)
        bao.destroy("eso", "acc-1")
        self.assertEqual(self.sent(), (
            "POST", "https://secrets/v1/auth/approle/role/eso/secret-id-accessor/destroy",
            {"secret_id_accessor": "acc-1"}))
        self.assertEqual(self.requests[-1].get_header("X-vault-token"), "tok")

    def test_lookup_sends_the_secret_id_in_the_body(self):
        bao = self.client(200, {"data": {"secret_id_accessor": "acc-1"}})
        self.assertEqual(bao.accessor_of("jenkins", "SECRET-x"), "acc-1")
        method, url, body = self.sent()
        self.assertEqual((method, url), (
            "POST", "https://secrets/v1/auth/approle/role/jenkins/secret-id/lookup"))
        self.assertEqual(body, {"secret_id": "SECRET-x"})

    def test_lookup_of_no_such_secret_id_is_none(self):
        self.assertIsNone(self.client(204, None).accessor_of("eso", "SECRET-x"))
        self.assertIsNone(self.client(404, {"errors": []}).accessor_of("eso", "SECRET-x"))

    def test_a_failed_lookup_does_not_echo_the_answer(self):
        bao = self.client(400, {"errors": ["invalid secret_id SECRET-x"]})
        with self.assertRaises(ac.BaoError) as e:
            bao.accessor_of("eso", "SECRET-x")
        self.assertEqual(str(e.exception),
                         "POST auth/approle/role/eso/secret-id/lookup: HTTP 400")

    def test_other_failures_carry_openbaos_errors(self):
        with self.assertRaises(ac.BaoError) as e:
            self.client(403, {"errors": ["permission denied"]}).destroy("eso", "acc-1")
        self.assertIn("HTTP 403: permission denied", str(e.exception))

    def test_accessors_are_listed_and_none_is_404(self):
        bao = self.client(200, {"data": {"keys": ["a", "b"]}})
        self.assertEqual(bao.accessors("eso"), ["a", "b"])
        self.assertEqual(self.sent()[:2],
                         ("LIST", "https://secrets/v1/auth/approle/role/eso/secret-id"))
        self.assertEqual(self.client(404, {"errors": []}).accessors("eso"), [])

    def test_mint_posts_to_the_roles_secret_id(self):
        bao = self.client(200, {"data": {"secret_id": "S", "secret_id_accessor": "A"}})
        self.assertEqual(bao.mint("jenkins"), ("S", "A"))
        self.assertEqual(self.sent(), (
            "POST", "https://secrets/v1/auth/approle/role/jenkins/secret-id", {}))


class Main(unittest.TestCase):
    def test_the_fallback_needs_apply(self):
        with mock.patch.dict(os.environ, {"BAO_TOKEN": "t"}), \
                mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as e:
            ac.main(["--jenkins-fallback"])
        self.assertEqual(e.exception.code, 2)

    def test_the_fallback_needs_the_jenkins_role(self):
        with mock.patch.dict(os.environ, {"BAO_TOKEN": "t"}), \
                mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as e:
            ac.main(["--apply", "--jenkins-fallback", "--role", "eso"])
        self.assertEqual(e.exception.code, 2)

    def test_a_missing_token_is_a_usage_error(self):
        env = {k: v for k, v in os.environ.items() if k != "BAO_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as e:
            ac.main([])
        self.assertEqual(e.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
