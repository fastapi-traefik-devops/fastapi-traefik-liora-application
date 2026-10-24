# CI implementation

See [the operating guide](../infra/jenkins-host/README.md). Scripts in this directory are
executed by Jenkins; only the trusted promotion job receives write credentials.

## Gate profiles and the main pipeline

Gates are defined once in `phase1.py`; the `Jenkinsfile` picks the profile:

| Trigger | Profile | Publishes |
| --- | --- | --- |
| Pull request | `pr`: **temporarily** `full` without `backend-integration` and `frontend-tests` | no |
| Nightly on main (`H 2 * * *`) | `full`: all 18 gates plus `gitops` | no |
| New commit on main (SCM poll `H/2`) | `release`: artifact, security and image gates, then `attest` | yes, to STAGE |
| Manual job `dev-build` (`BRANCH` parameter, `Jenkinsfile.dev`) | `dev`: the `pr` gates plus `attest` | yes, to DEV |

`dev-build` builds the real head commit of any branch, attests it like a release and asks
`trusted/publish` (`ACTION=dev`) to publish the scanned images and commit them directly to the
DEV overlay in infrastructure `main`; Argo CD deploys. DEV is never superseded and has no
acceptance. The `pr` profile is a temporary speed-up; delete it to return pull requests to `full`.

Source-level tests (lint, unit, integration, e2e) already passed on the pull request for the
same tree, so `release` skips them and re-verifies what gets published: secrets, dependency
audits, image builds, non-root smoke tests and Trivy, recorded by `attest` in `gates.json`.
The `gitops` gate validates the current infrastructure `main` overlays with this change's
promotion validator, so a new operator kind or CRD fails a pull request instead of STAGE.
Both triggers are declared in the `Jenkinsfile`, which replaces triggers set in the job UI.

The separate `trusted/publish` job accepts main builds only with a release attestation,
publishes immutable images and updates only the STAGE overlay in the infrastructure
repository; Argo CD deploys that update. A read-only `stage-observer` agent then checks
Argo, the running image digests and the STAGE URL, and the publisher records the result
as `acceptance.json`. Functional-only candidates are accepted solely from the manual
`migration-ci` checkpoint below. PROD stays blocked until acceptance is verified; images
are never rebuilt for PROD. See [publication details](PUBLICATION.md) and the
[explicit archive contract](ARTIFACTS.md).

## Manual K3s migration checkpoint

`Jenkinsfile.migration` is an operator-installed, inline, manual-only Pipeline
for `host-ci`. Its `APP_COMMIT` must be an exact reviewed commit; `SOURCE_BUNDLE`
is a local Git bundle supplied on the CI work volume. This operator-held mode needs no
GitHub egress and binds no publishing credentials. Other jobs must remain held
while using this separate one-shot checkpoint; it is not the normal main poll. The infrastructure operator helper requests exactly one build
and restores the execution hold after it finishes or fails.

`python3 ci/phase1.py steps --profile migration` selects 13 existing functional
gates. Security scans, GitOps validation and release attestation are deferred.
Dependency installation uses `npm ci --no-audit`; explicit security steps remain
available in the unchanged full/release profiles. Tests create disposable Docker
PostgreSQL data and never connect to the application K3s databases.

JUnit/coverage reports and `migration-result.json` record actual outcomes and
deferred gates. By default this is not publication evidence: no `gates.json` or
image tarballs are produced. The explicit `EXPORT_IMAGES=true` option exports
tested archives and a functional candidate with deferred-scan evidence, but still
never publishes, promotes or deploys. The infrastructure helper binds this option
to a new `prepare --export-images` receipt; an existing build cannot be relabeled
as a candidate. A failure ends that one-shot request, with evidence retained.

The manual Pipeline generates its result through `python3 ci/phase1.py migration-report`.
This records incomplete functional steps separately from deferred checks; XML
collection, archiving and cleanup also run if result generation fails.
