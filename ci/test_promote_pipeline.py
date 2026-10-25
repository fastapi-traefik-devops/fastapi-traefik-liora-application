"""Static checks for the trusted publisher's STAGE acceptance wiring in Jenkinsfile.promote."""
from pathlib import Path
import re
import unittest

PIPELINE = (Path(__file__).resolve().parents[1] / 'Jenkinsfile.promote').read_text()


def block(text, opening):
    # Return the brace-balanced body that follows the first occurrence of `opening`.
    start = text.index('{', text.index(opening)) + 1
    depth = 1
    for position in range(start, len(text)):
        depth += {'{': 1, '}': -1}.get(text[position], 0)
        if depth == 0:
            return text[start:position]
    raise AssertionError(f'Unbalanced block after {opening}')


class PromotePipelineTests(unittest.TestCase):
    def test_observer_runs_only_for_promoted_stage_inside_the_lock(self):
        locked = block(PIPELINE, "lock(resource: 'liora-stage-promotion')")
        self.assertIn("node('stage-observer')", locked)
        self.assertIn("params.ACTION == 'stage' && readJSON(file: 'stage-request.json').status == 'promoted'", locked)

    def test_observer_never_receives_credentials(self):
        observer = block(PIPELINE, "node('stage-observer')")
        self.assertNotIn('withCredentials', observer)
        self.assertNotIn('publisher-action.sh', observer)
        self.assertIn('stage_acceptance.py', observer)

    def test_receipt_is_written_by_trusted_helper_then_failure_fails_the_build(self):
        locked = block(PIPELINE, "lock(resource: 'liora-stage-promotion')")
        record = locked.index('publisher-action.sh accept-stage')
        verdict = locked.index("status != 'accepted'")
        self.assertLess(locked.index("node('stage-observer')"), record)
        self.assertLess(record, verdict)
        self.assertRegex(locked[verdict:], r"error\('STAGE acceptance failed")

    def test_each_action_has_its_source_and_prod_is_a_manual_release(self):
        choices = re.search(r"name: 'ACTION', choices: \[([^\]]*)\]", PIPELINE).group(1)
        self.assertEqual(choices, "'publish-only', 'stage', 'dev', 'prod'")
        self.assertIn("if (params.ACTION == 'dev' && params.SOURCE_JOB != 'dev-build')", PIPELINE)
        self.assertIn("if (params.ACTION == 'stage' && params.SOURCE_JOB != 'app-ci/main')", PIPELINE)

    def test_prod_needs_sha_and_tag_and_skips_source_artifacts(self):
        verify = PIPELINE[PIPELINE.index("stage('Verify source CI status and artifacts')"):PIPELINE.index("stage('Publish, update and accept")]
        prod = verify[verify.index("if (params.ACTION == 'prod')"):verify.index("return\n")]
        self.assertIn("params.APP_SHA ==~ /[0-9a-f]{40}/", prod)
        self.assertIn("params.RELEASE_TAG ==~ /v(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)/", prod)
        self.assertIn("env.EXPECTED_SOURCE_SHA = params.APP_SHA", prod)
        # The early return comes before any source-build lookup or artifact copy.
        self.assertLess(verify.index("return\n"), verify.index("copyArtifacts"))
        self.assertLess(verify.index("return\n"), verify.index("sourceBuild("))

    def test_prod_reports_the_review_branch_and_never_runs_the_observer(self):
        locked = block(PIPELINE, "lock(resource: 'liora-stage-promotion')")
        self.assertIn("prod.status != 'review-required' || !prod.review_url", locked)
        self.assertIn("if (prod.status == 'unchanged')", locked)
        self.assertIn("node('stage-observer')", locked)
        self.assertIn("if (params.ACTION == 'stage' && readJSON(file: 'stage-request.json').status == 'promoted')", locked)

    def test_timeout_covers_the_observation_window(self):
        minutes = int(re.search(r'timeout\(time: (\d+), unit: .MINUTES.\)', PIPELINE).group(1))
        self.assertGreaterEqual(minutes, 15 + 30)

    def test_evidence_is_archived(self):
        self.assertIn('stage-acceptance.json', block(PIPELINE, 'post'))


    def test_main_publishes_only_release_attested_candidates(self):
        self.assertIn("env.PUBLISH_MODE = params.SOURCE_JOB == 'migration-ci' ? 'functional' : 'release'", PIPELINE)
        self.assertIn('--mode "$PUBLISH_MODE"', PIPELINE)
        self.assertNotIn('--mode functional', PIPELINE)
        copied = re.search(r"filter: '([^']+)'", PIPELINE).group(1).split(',')
        self.assertIn('.ci-output/gates.json', copied)

if __name__ == '__main__':
    unittest.main()
