"""Gate profiles and attestation, on a temporary Git repository; no Docker or network."""
import json
import os
from unittest.mock import patch
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import phase1
from phase1 import GATES, PROFILES, RELEASE_GATES, STEPS

ROOT = Path(__file__).resolve().parents[1]


class ProfileTests(unittest.TestCase):
    def test_full_profile_runs_every_gate_and_gitops(self):
        self.assertEqual(PROFILES["full"], (*GATES, "gitops"))
        self.assertEqual(len(GATES), 18)

    def test_release_profile_keeps_artifact_and_supply_chain_gates(self):
        self.assertEqual(PROFILES["release"][-1], "attest")
        for gate in ("secrets", "backend-security", "frontend-security", "backend-image-smoke",
                     "frontend-image-smoke", "backend-image-security", "frontend-image-security"):
            self.assertIn(gate, RELEASE_GATES)
        for gate in ("backend-unit", "backend-integration", "frontend-tests"):
            self.assertNotIn(gate, RELEASE_GATES)

    def test_migration_only_runs_functional_gates_in_original_order(self):
        deferred = {"secrets", "backend-security", "frontend-security",
                    "backend-image-security", "frontend-image-security", "attest"}
        self.assertEqual(PROFILES["migration"], tuple(s for s in STEPS if s not in deferred))
        self.assertEqual(len(PROFILES["migration"]), 13)
        self.assertNotIn("gitops", PROFILES["migration"])
        self.assertFalse(RELEASE_GATES <= set(PROFILES["migration"]))

    def test_temporary_pr_profile_skips_only_slow_tests(self):
        self.assertEqual(set(PROFILES["full"]) - set(PROFILES["pr"]), {"backend-integration", "frontend-tests"})
        self.assertTrue(RELEASE_GATES <= set(PROFILES["pr"]))
        self.assertIn("backend-unit", PROFILES["pr"])

    def test_dev_profile_matches_release_without_source_tests(self):
        self.assertEqual(PROFILES["dev"], PROFILES["release"])
        for gate in ("backend-lint", "backend-unit", "frontend-lint", "frontend-build"):
            self.assertNotIn(gate, PROFILES["dev"])

    def test_profiles_only_use_known_steps(self):
        for steps in PROFILES.values():
            self.assertTrue(set(steps) <= {*STEPS, "gitops"})
            self.assertEqual(len(steps), len(set(steps)))

    def test_steps_command_prints_the_profile(self):
        output = subprocess.check_output(["python3", "-B", str(ROOT / "ci/phase1.py"), "steps", "--profile", "release"],
                                         text=True)
        self.assertEqual(tuple(output.split()), PROFILES["release"])


class BackendSelectionTests(unittest.TestCase):
    def setUp(self):
        self.ci = phase1.CI.__new__(phase1.CI)
        self.ci.name = "test-selection"

    def test_unit_selection_does_not_require_database(self):
        with patch.object(self.ci, "database") as database, patch.object(self.ci, "container") as container:
            self.ci.backend_tests(unit=True)
        database.assert_not_called()
        container.assert_called_once()
        command = container.call_args.args
        selection = command[command.index("pytest") + 1:command.index("-p")]
        self.assertEqual(selection, ("app/tests/scripts",))

    def test_integration_selection_prepares_database_and_includes_recovery(self):
        with patch.object(self.ci, "database") as database, patch.object(self.ci, "container") as container:
            self.ci.backend_tests(unit=False)
        database.assert_called_once_with()
        self.assertEqual(container.call_args_list[0].args,
                         (self.ci.test_image, "bash", "scripts/prestart.sh"))
        command = container.call_args_list[1].args
        selection = command[command.index("pytest") + 1:command.index("-p")]
        self.assertEqual(selection, ("app/tests/api", "app/tests/crud", "app/tests/integration"))
        self.assertIn("--append", command)


class GitopsGateTests(unittest.TestCase):
    def test_gitops_validates_a_fresh_infrastructure_main_checkout(self):
        calls = []
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "ci").mkdir()
            ci = phase1.CI.__new__(phase1.CI)
            ci.root, ci.state = Path(root), {"passed": []}
            ci.state_file = Path(root) / "state.json"
            ci.image = lambda prefix: prefix
            with patch.object(phase1, "run", side_effect=lambda *a, **k: calls.append((a, k.get("cwd")))):
                ci.step("gitops")
        clone, validate = calls[0], calls[1]
        self.assertEqual(clone[0][:3], ("git", "clone", "--quiet"))
        self.assertIn(phase1.INFRASTRUCTURE_REPO, clone[0])
        self.assertEqual(validate[0], ("sh", str(Path(root) / "ci/validate.sh")))
        self.assertEqual(str(validate[1]), clone[0][-1])
        self.assertIn("gitops", ci.state["passed"])


class AttestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "infra/jenkins-host").mkdir(parents=True)
        shutil.copy(ROOT / "infra/jenkins-host/images.lock.json", self.root / "infra/jenkins-host/")
        for args in (["init", "-q"], ["-c", "user.name=T", "-c", "user.email=t@example.invalid",
                                      "commit", "-q", "--allow-empty", "-m", "fixture"]):
            subprocess.run(["git", "-C", str(self.root), *args], check=True)
        self.ci = phase1.CI(self.root)
        reports = self.ci.reports
        for name in ("secrets-history.json", "secrets-tree.json"):
            (reports / name).write_text("[]")
        for component in ("backend", "frontend"):
            (reports / f"{component}-image-security.json").write_text(json.dumps({"Results": []}))
            (reports / f"{component}-ci.tar").write_bytes(component.encode())

    def test_export_saves_only_tested_runtime_images_with_no_scanner(self):
        self.ci.state['passed'] = list(PROFILES['migration'])
        for filename in phase1.TEST_REPORTS.values():
            (self.ci.reports / filename).write_text('<testsuite tests="2" failures="0" errors="0"/>')
        calls = []
        def save(*args):
            calls.append(args)
            self.assertEqual(args[:3], ('docker', 'save', '-o'))
            Path(args[3]).write_bytes(args[4].encode())
        with patch.object(phase1, 'run', side_effect=save), patch.dict(os.environ, JOB_NAME='migration-ci', BUILD_NUMBER='3'):
            self.ci.export_images()
        self.assertEqual([args[4] for args in calls], [f'phase1/{n}:{self.ci.name}' for n in ('backend', 'frontend')])
        manifest = json.loads((self.ci.reports / 'candidate.json').read_text())
        self.assertEqual(manifest['sha'], self.ci.sha)
        self.assertFalse(manifest['security_scanned'])
        self.assertFalse((self.ci.reports / 'gates.json').exists())

    def test_export_incomplete_gates_never_touches_docker(self):
        self.ci.state['passed'] = list(PROFILES['migration'][:-1])
        with patch.object(phase1, 'run') as command, self.assertRaises(RuntimeError):
            self.ci.export_images()
        command.assert_not_called()

    def attest_after(self, passed):
        self.ci.state["passed"] = list(passed)
        self.ci.step("attest")
        return json.loads((self.ci.reports / "gates.json").read_text())

    def test_migration_report_keeps_incomplete_and_deferred_steps_explicit(self):
        self.ci.state["passed"] = list(PROFILES["migration"][:3])
        self.ci.migration_report()
        report = json.loads((self.ci.reports / "migration-result.json").read_text())
        self.assertEqual(report["sha"], self.ci.sha)
        self.assertEqual(report["passed"], list(PROFILES["migration"][:3]))
        self.assertEqual(report["not_completed"], list(PROFILES["migration"][3:]))
        self.assertIn("attest", report["deferred"])
        self.assertIn("gitops", report["deferred"])
        self.assertFalse(report["publishable"])
        self.assertFalse((self.ci.reports / "gates.json").exists())

    def test_release_run_attests_release_gates(self):
        manifest = self.attest_after(RELEASE_GATES)
        self.assertEqual(set(manifest["gates"]), RELEASE_GATES)
        self.assertEqual(manifest["sha"], self.ci.sha)

    def test_full_run_attests_all_18_gates(self):
        passing = '<testsuite><testcase name="ok"/></testsuite>'
        for name in phase1.TEST_REPORTS.values():
            (self.ci.reports / name).write_text(passing)
        manifest = self.attest_after([*GATES, "gitops"])
        self.assertEqual(manifest["gates"], sorted(GATES))

    def test_missing_release_gate_refuses_attestation(self):
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.attest_after(RELEASE_GATES - {"backend-image-security"})

    def test_passed_test_gate_needs_its_report(self):
        with self.assertRaises(FileNotFoundError):
            self.attest_after([*RELEASE_GATES, "backend-unit"])

    def test_failing_test_report_refuses_attestation(self):
        (self.ci.reports / "backend-unit.xml").write_text('<testsuite><testcase name="x"><failure/></testcase></testsuite>')
        with self.assertRaisesRegex(RuntimeError, "failing test results"):
            self.attest_after([*RELEASE_GATES, "backend-unit"])


if __name__ == "__main__":
    unittest.main()
