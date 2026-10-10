"""check-terraform-drift.sh: a prd plan that only re-renders cloud-init snippets is not drift.

Each test writes a plan in the shape of `terraform show -json` (format 1.2, as Terraform 1.16
emits it) and runs the script on it. The snippet-only plan mirrors a real prd plan over a
changed ansible.pub (2026-10-10): every from-scratch VM's
proxmox_virtual_environment_file.cloud_init replaced ("delete","create"), every other resource
and output no-op.

Run: python3 -m unittest discover -s support/iac-agent/tests
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "check-terraform-drift.sh"

NO_OP = ["no-op"]
REPLACE = ["delete", "create"]


def snippet(name, actions=REPLACE):
    return {
        "address": f'proxmox_virtual_environment_file.cloud_init["{name}"]',
        "mode": "managed",
        "type": "proxmox_virtual_environment_file",
        "name": "cloud_init",
        "index": name,
        "change": {"actions": actions},
    }


def vm(name, actions=NO_OP):
    return {
        "address": f'module.vm["{name}"].proxmox_virtual_environment_vm.vm',
        "module_address": f'module.vm["{name}"]',
        "mode": "managed",
        "type": "proxmox_virtual_environment_vm",
        "name": "vm",
        "change": {"actions": actions},
    }


def plan(resources, outputs=None):
    return {
        "format_version": "1.2",
        "resource_changes": resources,
        "output_changes": outputs
        if outputs is not None
        else {name: {"actions": NO_OP} for name in ("vm_ids", "nic_macs", "host_pubkeys")},
    }


class CheckTerraformDrift(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def run_script(self, *args):
        return subprocess.run(
            ["bash", str(SCRIPT), *args], capture_output=True, text=True
        )

    def check(self, body):
        path = self.dir / "plan.json"
        path.write_text(json.dumps(body))
        return self.run_script(str(path))

    def test_a_plan_that_only_re_renders_snippets_passes_and_says_so(self):
        result = self.check(
            plan([snippet("srvk8s1"), snippet("srvvault1"), vm("srvk8s1"), vm("srvvault1")])
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not drift", result.stdout)
        self.assertIn('proxmox_virtual_environment_file.cloud_init["srvk8s1"]', result.stdout)
        self.assertIn('proxmox_virtual_environment_file.cloud_init["srvvault1"]', result.stdout)
        self.assertNotIn("DRIFT", result.stdout + result.stderr)

    def test_a_vm_change_beside_the_snippets_fails_and_is_named(self):
        result = self.check(plan([snippet("srvk8s1"), vm("srvk8s1", ["update"])]))
        self.assertEqual(result.returncode, 1)
        self.assertIn("DRIFT: terraform plan proposes changes against prd", result.stderr)
        self.assertIn(
            'plan changes module.vm["srvk8s1"].proxmox_virtual_environment_vm.vm (update)',
            result.stderr,
        )
        self.assertNotIn("cloud_init", result.stderr)

    def test_another_file_resource_fails(self):
        other = snippet("srvk8s1")
        other["name"] = "other"
        other["address"] = 'proxmox_virtual_environment_file.other["srvk8s1"]'
        result = self.check(plan([snippet("srvk8s2"), other]))
        self.assertEqual(result.returncode, 1)
        self.assertIn("proxmox_virtual_environment_file.other", result.stderr)

    def test_a_cloud_init_file_inside_a_module_fails(self):
        nested = snippet("srvk8s1")
        nested["module_address"] = "module.other"
        nested["address"] = 'module.other.proxmox_virtual_environment_file.cloud_init["srvk8s1"]'
        result = self.check(plan([nested]))
        self.assertEqual(result.returncode, 1)
        self.assertIn("module.other.proxmox_virtual_environment_file.cloud_init", result.stderr)

    def test_an_output_change_beside_the_snippets_fails(self):
        outputs = {"vm_ids": {"actions": ["update"]}, "nic_macs": {"actions": NO_OP}}
        result = self.check(plan([snippet("srvk8s1")], outputs))
        self.assertEqual(result.returncode, 1)
        self.assertIn("plan changes output.vm_ids (update)", result.stderr)
        self.assertNotIn("nic_macs", result.stderr)

    def test_a_data_source_read_fails(self):
        read = {
            "address": "data.http.example",
            "mode": "data",
            "type": "http",
            "name": "example",
            "change": {"actions": ["read"]},
        }
        result = self.check(plan([snippet("srvk8s1"), read]))
        self.assertEqual(result.returncode, 1)
        self.assertIn("data.http.example (read)", result.stderr)

    def test_a_plan_that_lists_no_change_fails(self):
        result = self.check(plan([vm("srvk8s1")]))
        self.assertEqual(result.returncode, 1)
        self.assertIn("DRIFT", result.stderr)

    def test_usage_and_unreadable_plans_exit_2(self):
        self.assertEqual(self.run_script().returncode, 2)
        self.assertEqual(self.run_script("a", "b").returncode, 2)
        self.assertEqual(self.run_script(str(self.dir / "missing.json")).returncode, 2)
        broken = self.dir / "broken.json"
        broken.write_text("{not json")
        self.assertEqual(self.run_script(str(broken)).returncode, 2)


if __name__ == "__main__":
    unittest.main()
