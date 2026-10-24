// CI for the multibranch job app-ci: main and pull requests. Gate profiles live in ci/phase1.py:
//   pull request                      -> "pr" (TEMPORARY: "full" without integration and e2e tests)
//   nightly run on main               -> "full" (all 18 gates plus GitOps validation), no publication
//   change on main                    -> "release" (artifact, security and attestation gates), then
//                                        trusted/publish promotes the attested images to STAGE
pipeline {
    agent { label 'host-ci' }
    options {
        skipDefaultCheckout(true)
        disableConcurrentBuilds()
        timeout(time: 90, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '20', artifactNumToKeepStr: '2'))
        copyArtifactPermission('/trusted/publish')
    }
    // Declared here, so these replace any triggers set in the job UI: main polls GitHub every
    // two minutes (Jenkins is private, no webhooks) and runs the full profile nightly.
    triggers {
        pollSCM(env.BRANCH_NAME == 'main' ? 'H/2 * * * *' : '')
        cron(env.BRANCH_NAME == 'main' ? 'H 2 * * *' : '')
    }
    stages {
        stage('Checkout') {
            steps {
                script {
                    def revision = checkout scm
                    env.IMAGE_SHA = revision.GIT_COMMIT
                    if (!(env.IMAGE_SHA ==~ /[0-9a-f]{40}/)) { error('Full Git SHA required') }
                    def nightly = !currentBuild.getBuildCauses('hudson.triggers.TimerTrigger$TimerTriggerCause').isEmpty()
                    env.CI_PROFILE = env.CHANGE_ID ? 'pr' : ((nightly || env.BRANCH_NAME != 'main') ? 'full' : 'release')
                    env.PUBLISH = (env.BRANCH_NAME == 'main' && env.CI_PROFILE == 'release') ? 'true' : 'false'
                    currentBuild.description = "Profile ${env.CI_PROFILE}${nightly ? ' (nightly)' : ''}" +
                        "${env.PUBLISH == 'true' ? ', publishes to STAGE' : ''}"
                }
            }
        }
        stage('CI gates') {
            steps {
                script {
                    def gates = sh(returnStdout: true, script: "python3 ci/phase1.py steps --profile ${env.CI_PROFILE}").trim().split('\n')
                    for (gate in gates) { stage(gate) { sh "python3 ci/phase1.py ${gate}" } }
                }
            }
        }
        stage('Archive attested images') {
            when { expression { env.PUBLISH == 'true' } }
            steps {
                // The image-security gates saved the exact scanned archives; attest recorded them.
                archiveArtifacts artifacts: '.ci-output/*-ci.tar,.ci-output/gates.json,.ci-output/source-sha.txt', fingerprint: true
            }
        }
        stage('Request trusted STAGE publication') {
            when { expression { env.PUBLISH == 'true' } }
            steps {
                build job: '/trusted/publish', wait: false, parameters: [
                    string(name: 'CI_BUILD', value: env.BUILD_NUMBER),
                    string(name: 'SOURCE_JOB', value: 'app-ci/main'),
                    string(name: 'ACTION', value: 'stage')]
            }
        }
    }
    post {
        always {
            script {
                try {
                    for (report in ['backend-unit', 'backend-integration', 'frontend-tests']) {
                        if (fileExists(".ci-output/${report}.xml")) { junit ".ci-output/${report}.xml" }
                    }
                } finally {
                    try { archiveArtifacts artifacts: '.ci-output/*.json,.ci-output/*.xml', allowEmptyArchive: true }
                    finally {
                        try { if (fileExists('ci/phase1.py')) { sh 'python3 ci/phase1.py cleanup' } }
                        finally { deleteDir() }
                    }
                }
            }
        }
    }
}
