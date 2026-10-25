"""Static checks for profile selection, triggers and publication in the main Jenkinsfile."""
from pathlib import Path
import re
import unittest

PIPELINE = (Path(__file__).resolve().parents[1] / 'Jenkinsfile').read_text()


class JenkinsfileTests(unittest.TestCase):
    def test_pull_requests_and_nightly_run_full_and_main_runs_release(self):
        self.assertIn("env.CI_PROFILE = (env.CHANGE_ID || nightly || env.BRANCH_NAME != 'main') ? 'full' : 'release'", PIPELINE)
        self.assertNotIn("'pr'", PIPELINE)
        self.assertIn('hudson.triggers.TimerTrigger$TimerTriggerCause', PIPELINE)
        self.assertIn('python3 ci/phase1.py steps --profile ${env.CI_PROFILE}', PIPELINE)
        self.assertNotIn('--profile migration', PIPELINE)

    def test_only_release_runs_on_main_publish(self):
        self.assertIn("env.PUBLISH = (env.BRANCH_NAME == 'main' && env.CI_PROFILE == 'release') ? 'true' : 'false'", PIPELINE)
        self.assertEqual(PIPELINE.count("when { expression { env.PUBLISH == 'true' } }"), 2)

    def test_main_polls_and_runs_nightly_other_branches_have_no_triggers(self):
        triggers = re.search(r'triggers \{(.*?)\n    \}', PIPELINE, re.S).group(1)
        self.assertIn("pollSCM(env.BRANCH_NAME == 'main' ? 'H/2 * * * *' : '')", triggers)
        self.assertIn("cron(env.BRANCH_NAME == 'main' ? 'H 2 * * *' : '')", triggers)

    def test_publication_hands_over_the_attested_archives(self):
        archived = re.search(r"stage\('Archive attested images'\).*?archiveArtifacts artifacts: '([^']+)'", PIPELINE, re.S).group(1)
        self.assertEqual(set(archived.split(',')), {'.ci-output/*-ci.tar', '.ci-output/gates.json', '.ci-output/source-sha.txt'})
        self.assertNotIn('export-images', PIPELINE)



DEV = (Path(__file__).resolve().parents[1] / 'Jenkinsfile.dev').read_text()


class DevJenkinsfileTests(unittest.TestCase):
    def test_builds_the_real_branch_head_with_the_dev_profile(self):
        self.assertIn("string(name: 'BRANCH', defaultValue: 'main'", DEV)
        self.assertIn('branches: [[name: "refs/heads/${params.BRANCH}"]]', DEV)
        self.assertIn("python3 ci/phase1.py steps --profile dev", DEV)
        self.assertIn("agent { label 'host-ci' }", DEV)

    def test_fetches_only_the_selected_branch(self):
        self.assertIn('refspec: "+refs/heads/${params.BRANCH}:refs/remotes/origin/${params.BRANCH}"', DEV)
        self.assertIn("honorRefspec: true, noTags: true", DEV)

    def test_rejects_unsafe_branch_names(self):
        self.assertIn("if (!(params.BRANCH ==~ /[A-Za-z0-9._\\/-]+/) || params.BRANCH.contains('..'))", DEV)

    def test_hands_attested_archives_to_the_trusted_dev_action_without_credentials(self):
        self.assertNotIn('withCredentials', DEV)
        self.assertIn("'.ci-output/*-ci.tar,.ci-output/gates.json,.ci-output/source-sha.txt'", DEV)
        self.assertIn("string(name: 'SOURCE_JOB', value: 'dev-build')", DEV)
        self.assertIn("string(name: 'ACTION', value: 'dev')", DEV)
        self.assertIn("copyArtifactPermission('/trusted/publish')", DEV)

if __name__ == '__main__':
    unittest.main()
