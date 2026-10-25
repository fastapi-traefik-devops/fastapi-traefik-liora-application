"""Exercise credential handling with inert trusted helpers and fake Git/registry."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class PublisherActionTests(unittest.TestCase):
    def invoke(self, action='stage', missing=None, newest='a' * 40, source_job='app-ci/main', release=''):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(Path(__file__).with_name('publisher-action.sh'), root)
            (root / 'registry.py').write_text('import sys; print("validated candidate", " ".join(sys.argv[2:4]))\n')
            (root / 'promote.py').write_text('''import os, subprocess, sys
assert subprocess.check_output([os.environ['GIT_ASKPASS'], 'Password'], text=True).strip() == 'fixture-git-secret'
print('promote ' + ' '.join(sys.argv[1:]))
''')
            for name, body in [('git', 'printf "%s\\trefs/heads/main\\n" "$NEWEST"'), ('crane', 'cat >/dev/null')]:
                (root / name).write_text('#!/bin/sh\n' + body + '\n')
                (root / name).chmod(0o700)
            env = dict(os.environ, PATH=str(root) + ':' + os.environ['PATH'], NEWEST=newest,
                       ACTION=action, EXPECTED_SOURCE_SHA='a' * 40, GHCR_USER='fake',
                       GHCR_TOKEN='fixture-registry-secret', GIT_TOKEN='fixture-git-secret',
                       SOURCE_JOB=source_job, CI_BUILD='3', RELEASE_TAG=release)
            if missing: env.pop(missing, None)
            result = subprocess.run(['sh', str(root / 'publisher-action.sh')], cwd=root, env=env, text=True, capture_output=True)
            output = result.stdout + result.stderr
            self.assertNotIn('fixture-git-secret', output)
            self.assertNotIn('fixture-registry-secret', output)
            return result.returncode, output

    def test_stage_validates_then_promotes(self):
        status, output = self.invoke()
        self.assertEqual(status, 0)
        self.assertLess(output.index('validated candidate'), output.index('promote stage'))

    def test_missing_credentials_fail_before_publication(self):
        for name in ('GIT_TOKEN', 'GHCR_TOKEN', 'EXPECTED_SOURCE_SHA'):
            status, output = self.invoke(missing=name)
            self.assertNotEqual(status, 0)
            self.assertNotIn('validated candidate', output)

    def test_superseded_candidate_is_not_published(self):
        status, output = self.invoke(newest='b' * 40)
        self.assertEqual(status, 0)
        self.assertNotIn('validated candidate', output)
        self.assertNotIn('promote stage', output)

    def test_prod_requires_a_release_tag(self):
        for tag in ('', '1.0.0', 'v1.0', 'v01.0.0', 'v1.0.0-rc1'):
            status, output = self.invoke(action='prod', release=tag)
            self.assertNotEqual(status, 0, tag)
            self.assertNotIn('promote prod', output)

    def test_prod_reuses_published_images_without_registry_publication(self):
        status, output = self.invoke(action='prod', release='v1.0.0')
        self.assertEqual(status, 0)
        self.assertNotIn('validated candidate', output)
        self.assertIn('promote prod ' + 'a' * 40 + ' --release v1.0.0', output)

    def test_main_requires_release_attestation(self):
        status, output = self.invoke(source_job='app-ci/main')
        self.assertEqual(status, 0)
        self.assertIn('validated candidate --mode release', output)

    def test_only_migration_checkpoint_may_publish_functional_candidates(self):
        status, output = self.invoke(action='publish-only', source_job='migration-ci')
        self.assertEqual(status, 0)
        self.assertIn('validated candidate --mode functional', output)

    def test_unknown_source_job_is_rejected_before_registry(self):
        status, output = self.invoke(source_job='app-ci/PR-1')
        self.assertNotEqual(status, 0)
        self.assertNotIn('validated candidate', output)

    def test_dev_from_dev_build_requires_release_attestation(self):
        status, output = self.invoke(action='dev', source_job='dev-build')
        self.assertEqual(status, 0)
        self.assertIn('validated candidate --mode release', output)
        self.assertIn('promote dev', output)

    def test_actions_only_accept_their_own_source(self):
        for action, job in (('dev', 'app-ci/main'), ('stage', 'dev-build'), ('dev', 'migration-ci')):
            status, output = self.invoke(action=action, source_job=job)
            self.assertNotEqual(status, 0, (action, job))
            self.assertNotIn('validated candidate', output)
