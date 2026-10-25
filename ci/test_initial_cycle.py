"""Exercise attested main build -> immutable publication -> split-repository STAGE update."""
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import candidate
from phase1 import PROFILES, RELEASE_GATES, TEST_REPORTS, file_sha
import registry
import test_promote as fixtures


class MainCycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PromotionTests('test_stage_is_scoped_and_idempotent')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.artifacts = self.fixture.root / 'artifacts'
        self.artifacts.mkdir()
        for name in ('backend', 'frontend'):
            (self.artifacts / f'{name}-ci.tar').write_bytes(('scanned-runtime-' + name).encode())
        (self.artifacts / 'source-sha.txt').write_text(self.fixture.sha + '\n')

    def attest(self, gates):
        manifest = {'version': 1, 'sha': self.fixture.sha, 'gates': sorted(gates),
                    'archives': {n: file_sha(self.artifacts / f'{n}-ci.tar') for n in ('backend', 'frontend')}}
        (self.artifacts / 'gates.json').write_text(json.dumps(manifest))

    def publish(self, mode='release', job='app-ci/main'):
        remote = {}
        def archive_digest(args, **kwargs):
            return self.fixture.pair[Path(args[3]).name.removesuffix('-ci.tar')] + '\n'
        def command(args, *a, **kwargs):
            if args[0] != 'crane':
                return fixtures.REAL_RUN(args, *a, **kwargs)
            remote[args[3]] = self.fixture.pair[Path(args[2]).name.removesuffix('-ci.tar')]
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch.object(registry, 'digest', side_effect=lambda tool, image: remote.get(image)), \
             patch.object(registry.subprocess, 'check_output', side_effect=archive_digest), \
             patch.object(registry.subprocess, 'run', side_effect=command):
            registry.publish(self.artifacts, self.fixture.sha, 'ghcr.io/fastapi-traefik-devops',
                             mode=mode, source_job=job, source_build='4')
        return json.loads((self.artifacts / 'published-images.json').read_text())

    def test_release_attested_main_build_publishes_and_promotes_stage(self):
        self.attest(RELEASE_GATES)
        published = self.publish()
        self.assertEqual({n: v['digest'] for n, v in published.items()}, self.fixture.pair)
        result = self.fixture.invoke('stage')
        self.assertEqual(result['status'], 'promoted')
        recorded = json.loads(self.fixture.show('kubernetes/apps/liora/overlays/stage/promotion.json'))
        self.assertEqual((recorded['sha'], recorded['digests']), (self.fixture.sha, self.fixture.pair))
        # A desired-state update is not accepted-deployment evidence.
        self.fixture.tag()
        with self.assertRaisesRegex(RuntimeError, 'requires successful STAGE acceptance'):
            self.fixture.invoke('prod', future=True, release='v1.2.0')

    def test_main_without_security_gates_is_never_published(self):
        self.attest(RELEASE_GATES - {'backend-image-security'})
        with self.assertRaises(ValueError):
            self.publish()

    def test_main_cannot_downgrade_to_a_functional_candidate(self):
        for filename in TEST_REPORTS.values():
            (self.artifacts / filename).write_text('<testsuite tests="2" failures="0" errors="0"/>')
        with self.assertRaisesRegex(ValueError, 'Unapproved CI source'):
            candidate.write_candidate(self.artifacts, self.fixture.sha, PROFILES['migration'], 'app-ci/main', '4')
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.publish(mode='functional')


if __name__ == '__main__':
    unittest.main()
