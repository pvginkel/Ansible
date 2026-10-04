"""seed.yaml over store-keys.json: every leaf the store holds after slice 044's cutover resolves.

store-keys.json maps each of those leaves to its data key names (no values): the value-blind
inventory of 2026-10-04, less the 15 leaves the cutover deletes, plus the three Elasticsearch
leaves it creates. A leaf added to the seed is added there with its key names.

Run: python3 -m unittest discover -s scripts/rotation
"""

import json
import tempfile
import unittest
from pathlib import Path

import annotate as an

KEYS = Path(__file__).resolve().with_name("store-keys.json")


def check(keys_file: Path) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = an.main(["--check", "--keys", str(keys_file)], out=lines.append)
    return code, lines


class SeedOverTheStore(unittest.TestCase):
    def setUp(self):
        self.seed = an.load_seed(an.DEFAULT_SEED)
        self.store = json.loads(KEYS.read_text())

    def test_every_key_of_every_leaf_resolves(self):
        code, lines = check(KEYS)
        self.assertEqual(lines, [f"0 finding(s) on 0 of {len(self.store)} leaf(s)"])
        self.assertEqual(code, 0)

    def test_the_seed_covers_exactly_the_stores_leaves(self):
        self.assertEqual(sorted(self.seed), sorted(self.store))

    def test_it_holds_once_keycloak_da_admin_is_deleted(self):
        # ANS-229 deletes the leaf; the seed keeps it until then (ruling F2).
        del self.store["jenkins/keycloak-da-admin"]
        with tempfile.TemporaryDirectory() as tmp:
            keys = Path(tmp, "keys.json")
            keys.write_text(json.dumps(self.store))
            code, lines = check(keys)
        self.assertEqual(lines, ["seed leaf not in the key file: jenkins/keycloak-da-admin",
                                 f"0 finding(s) on 0 of {len(self.store)} leaf(s)"])
        self.assertEqual(code, 0)

    def test_the_per_key_cadences_of_ruling_q1(self):
        want = {
            "eso/prd/trello-mcp/prd/trello": {
                "rotation_mechanism": "manual", "rotation_interval": "never",
                "key_bearer-token": "random", "interval_bearer-token": "14d"},
            "iac/tf-backend": {
                "rotation_mechanism": "manual", "rotation_interval": "never",
                "interval_github_token": "365d"},
            "shared/samba/users": {
                "rotation_mechanism": "manual", "rotation_interval": "365d",
                "interval_mvdbovenkamp": "never"},
        }
        for leaf, keys in want.items():
            with self.subTest(leaf=leaf):
                self.assertEqual({k: self.seed[leaf].get(k) for k in keys}, keys)
                self.assertTrue(self.seed[leaf].get("notes", "").strip())
        self.assertIn("cannot be rotated", self.seed["eso/prd/trello-mcp/prd/trello"]["notes"])


if __name__ == "__main__":
    unittest.main()
