"""Independent application/infrastructure bare remotes; no GitHub/GHCR calls."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import promote

REAL_RUN = subprocess.run


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app, self.aw = self.repository('application')
        self.infra, self.iw = self.repository('infrastructure')
        (self.aw / 'source').write_text('first')
        self.sha = self.save(self.aw)
        for env in ('dev', 'stage', 'prod'):
            directory = self.iw / promote.OVERLAYS / env
            directory.mkdir(parents=True)
            (directory / 'kustomization.yaml').write_text('images:\n' + ''.join(
                f'- name: {n}\n  newName: {promote.REGISTRY}{n}\n  newTag: unconfigured\n' for n in promote.COMPONENTS))
        self.base = self.save(self.iw)
        self.pair = {n: 'sha256:' + c * 64 for n, c in [('backend', 'a'), ('frontend', 'b')]}
        self.validator = self.root / 'validate.sh'
        self.validator.write_text('#!/bin/sh\nexit 0\n')

    def repository(self, name):
        remote, work = self.root / (name + '.git'), self.root / name
        promote.git('init', '--bare', '--initial-branch=main', str(remote))
        promote.git('clone', str(remote), str(work))
        promote.git('config', 'user.name', 'Fixture', cwd=work)
        promote.git('config', 'user.email', 'fixture@example.invalid', cwd=work)
        return remote, work

    def save(self, work):
        promote.git('add', '.', cwd=work)
        promote.git('commit', '-qm', 'Fixture commit', cwd=work)
        promote.git('push', '-q', 'origin', 'main', cwd=work)
        return promote.git('rev-parse', 'HEAD', cwd=work)

    def invoke(self, env='stage', future=False, **kwargs):
        with patch.object(promote, 'PROD_ENABLED', future):
            return promote.promote(env, self.sha, self.pair, app_repo=str(self.app),
                                   infra_repo=str(self.infra), validator=self.validator, **kwargs)

    def show(self, path, branch='main'):
        return promote.git('--git-dir', str(self.infra), 'show', branch + ':' + path)

    def head(self):
        return promote.git('--git-dir', str(self.infra), 'rev-parse', 'main')

    def receipt(self, result):
        return {'version': 1, 'status': 'accepted', 'sha': self.sha, 'digests': self.pair,
                'infrastructure_commit': result['infrastructure_commit'], 'argo_revision': self.head(),
                'checks': {'argo': True, 'running_images': True, 'url': True}}

    def tag(self):
        promote.git('tag', 'v1.2.0', cwd=self.aw)
        promote.git('push', '-q', 'origin', 'v1.2.0', cwd=self.aw)

    def test_stage_is_scoped_and_idempotent(self):
        self.assertNotEqual(self.sha, self.base)
        result = self.invoke()
        head = self.head()
        self.assertEqual(result['status'], 'promoted')
        paths = set(promote.git('--git-dir', str(self.infra), 'diff-tree', '--no-commit-id', '--name-only', '-r', head).splitlines())
        self.assertTrue(paths <= promote.STAGE_FILES)
        self.assertEqual(json.loads(self.show(promote.OVERLAYS + '/stage/promotion.json'))['digests'], self.pair)
        self.invoke()
        self.assertEqual(head, self.head())

    def test_any_new_main_commit_even_docs_or_failed_ci_supersedes(self):
        (self.aw / 'README.md').write_text('newer failed or unbuilt commit')
        self.save(self.aw)
        self.assertEqual(self.invoke()['status'], 'superseded')
        self.assertEqual(self.head(), self.base)

    def test_recheck_immediately_before_push(self):
        real = promote.current_main
        calls = 0
        def advance(repo):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.aw / 'source').write_text('new main')
                self.save(self.aw)
            return real(repo)
        with patch.object(promote, 'current_main', side_effect=advance):
            self.assertEqual(self.invoke()['status'], 'superseded')
        self.assertEqual(self.head(), self.base)

    def test_unrelated_concurrent_infrastructure_change_survives(self):
        raced = False
        def race(args, *a, **kw):
            nonlocal raced
            if args[:3] == ['git', 'push', 'origin'] and not raced:
                raced = True
                (self.iw / 'notes.md').write_text('human update')
                self.save(self.iw)
            return REAL_RUN(args, *a, **kw)
        with patch.object(subprocess, 'run', side_effect=race):
            self.invoke()
        self.assertTrue(raced)
        self.assertEqual(self.show('notes.md'), 'human update')

    def test_rules_rejection_fails_once_and_redacts_transport(self):
        hook = self.infra / 'hooks/pre-receive'
        hook.write_text('#!/bin/sh\necho fixture-secret-token >&2\nexit 1\n')
        hook.chmod(0o755)
        with patch.object(promote, 'push_main', wraps=promote.push_main) as push:
            with self.assertRaisesRegex(RuntimeError, 'branch rules') as raised:
                self.invoke()
            self.assertNotIn('fixture-secret-token', str(raised.exception))
            self.assertEqual(push.call_count, 1)
        self.assertEqual(self.head(), self.base)

    def test_dev_commits_only_dev_files_directly_and_is_idempotent(self):
        result = self.invoke('dev')
        head = self.head()
        self.assertEqual((result['status'], result['infrastructure_commit']), ('promoted', head))
        paths = set(promote.git('--git-dir', str(self.infra), 'diff-tree', '--no-commit-id', '--name-only', '-r', head).splitlines())
        self.assertEqual(paths, {promote.OVERLAYS + '/dev/kustomization.yaml', promote.OVERLAYS + '/dev/promotion.json'})
        self.assertEqual(json.loads(self.show(promote.OVERLAYS + '/dev/promotion.json'))['sha'], self.sha)
        self.invoke('dev')
        self.assertEqual(head, self.head())

    def test_dev_accepts_a_branch_commit_that_is_not_on_main(self):
        promote.git('checkout', '-qb', 'feature', cwd=self.aw)
        (self.aw / 'source').write_text('feature work')
        promote.git('commit', '-qam', 'Feature', cwd=self.aw)
        promote.git('push', '-q', 'origin', 'feature', cwd=self.aw)
        self.sha = promote.git('rev-parse', 'HEAD', cwd=self.aw)
        self.assertEqual(self.invoke('dev')['status'], 'promoted')
        with self.assertRaises(RuntimeError):
            self.invoke('stage')

    def test_dev_never_touches_stage_acceptance(self):
        self.invoke()
        stage = self.show(promote.OVERLAYS + '/stage/promotion.json')
        self.invoke('dev')
        self.assertEqual(self.show(promote.OVERLAYS + '/stage/promotion.json'), stage)

    def test_stage_promotion_without_acceptance_cannot_qualify_prod(self):
        self.invoke()
        self.tag()
        with self.assertRaisesRegex(RuntimeError, 'successful STAGE acceptance'):
            self.invoke('prod', future=True, release='v1.2.0')

    def test_accepted_pair_qualifies_prod_without_build_or_main_write(self):
        result = self.invoke()
        promote.record_acceptance(self.receipt(result), str(self.infra))
        self.tag()
        before = self.head()
        prod = self.invoke('prod', future=True, release='v1.2.0')
        self.assertEqual(prod['status'], 'review-required')
        self.assertEqual(self.head(), before)
        self.assertEqual(json.loads(self.show(promote.OVERLAYS + '/prod/promotion.json', prod['branch']))['digests'], self.pair)

    def test_failed_or_wrong_digest_acceptance_never_qualifies(self):
        result = self.invoke()
        record = self.receipt(result)
        record['checks']['url'] = False
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            promote.record_acceptance(record, str(self.infra))
        record['status'] = 'failed'
        promote.record_acceptance(record, str(self.infra))
        self.tag()
        with self.assertRaisesRegex(RuntimeError, 'successful STAGE acceptance'):
            self.invoke('prod', future=True, release='v1.2.0')

    def test_receipt_for_other_pair_or_revision_rejected(self):
        result = self.invoke()
        record = self.receipt(result)
        record['digests'] = dict(self.pair, backend='sha256:' + 'f' * 64)
        with self.assertRaisesRegex(RuntimeError, 'obsolete'):
            promote.record_acceptance(record, str(self.infra))
        record = self.receipt(result)
        record['argo_revision'] = self.base
        with self.assertRaises(RuntimeError):
            promote.record_acceptance(record, str(self.infra))

    def test_new_promotion_invalidates_current_acceptance(self):
        first = self.invoke()
        promote.record_acceptance(self.receipt(first), str(self.infra))
        (self.aw / 'source').write_text('second')
        self.sha = self.save(self.aw)
        self.invoke()
        files = promote.git('--git-dir', str(self.infra), 'ls-tree', '-r', '--name-only', 'main').splitlines()
        self.assertNotIn(promote.OVERLAYS + '/stage/acceptance.json', files)

    def release(self, content, tag):
        # One accepted STAGE cycle for a new application SHA, tagged as a release.
        (self.aw / 'source').write_text(content)
        self.sha = self.save(self.aw)
        promote.record_acceptance(self.receipt(self.invoke()), str(self.infra))
        promote.git('tag', tag, cwd=self.aw)
        promote.git('push', '-q', 'origin', tag, cwd=self.aw)
        return self.sha

    def merge(self, branch):
        promote.git('fetch', '-q', 'origin', cwd=self.iw)
        promote.git('checkout', '-q', 'main', cwd=self.iw)
        promote.git('reset', '-q', '--hard', 'origin/main', cwd=self.iw)
        promote.git('merge', '-q', '--no-ff', '-m', 'Merge ' + branch, 'origin/' + branch, cwd=self.iw)
        promote.git('push', '-q', 'origin', 'main', cwd=self.iw)

    def prod_sha(self):
        return json.loads(self.show(promote.OVERLAYS + '/prod/promotion.json'))['sha']

    def test_rollback_re_releases_a_merged_tag_on_a_new_review_branch(self):
        old = self.release('v1', 'v1.0.0')
        first = self.invoke('prod', future=True, release='v1.0.0')
        self.merge(first['branch'])
        new = self.release('v2', 'v1.0.1')
        self.merge(self.invoke('prod', future=True, release='v1.0.1')['branch'])
        self.assertEqual(self.prod_sha(), new)
        self.sha = old
        rollback = self.invoke('prod', future=True, release='v1.0.0')
        self.assertEqual(rollback['status'], 'review-required')
        self.assertTrue(rollback['branch'].startswith('promotion/prod/v1.0.0-redeploy-'))
        self.merge(rollback['branch'])
        self.assertEqual(self.prod_sha(), old)

    def test_releasing_the_live_version_again_is_unchanged(self):
        self.release('v1', 'v1.0.0')
        self.merge(self.invoke('prod', future=True, release='v1.0.0')['branch'])
        self.assertEqual(self.invoke('prod', future=True, release='v1.0.0')['status'], 'unchanged')

    def test_prod_is_enabled_and_still_needs_acceptance(self):
        self.assertTrue(promote.PROD_ENABLED)
        self.tag()
        with self.assertRaisesRegex(RuntimeError, 'requires successful STAGE acceptance'):
            promote.promote('prod', self.sha, self.pair, release='v1.2.0', app_repo=str(self.app),
                            infra_repo=str(self.infra), validator=self.validator)

    def test_kill_switch_blocks_prod_even_with_accepted_fixture(self):
        result = self.invoke()
        promote.record_acceptance(self.receipt(result), str(self.infra))
        self.tag()
        with self.assertRaisesRegex(RuntimeError, 'PROD is blocked'):
            self.invoke('prod', release='v1.2.0')

    def test_release_tag_must_match_application_sha(self):
        self.tag()
        (self.aw / 'source').write_text('new')
        self.sha = self.save(self.aw)
        with self.assertRaisesRegex(RuntimeError, 'Release tag'):
            self.invoke('prod', future=True, release='v1.2.0')


if __name__ == '__main__':
    unittest.main()
