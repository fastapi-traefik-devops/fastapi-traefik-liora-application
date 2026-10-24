#!/usr/bin/env python3
"""Trusted two-repository writer. STAGE (automatic) and DEV (manual) update main; PROD needs review."""
import argparse
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import re
import subprocess
import tempfile

APPLICATION_REPO = 'https://github.com/fastapi-traefik-devops/fastapi-traefik-liora-application.git'
INFRASTRUCTURE_REPO = 'https://github.com/fastapi-traefik-devops/fastapi-traefik-liora-infrastructure.git'
REGISTRY = 'ghcr.io/fastapi-traefik-devops/fastapi-'
OVERLAYS = 'kubernetes/apps/liora/overlays'
# Flip only in a reviewed change after the agreed acceptance gate is live-verified.
PROD_ENABLED = False
COMPONENTS = ('backend', 'frontend')
STAGE_FILES = {f'{OVERLAYS}/stage/{n}' for n in ('kustomization.yaml', 'promotion.json', 'acceptance.json')}


def run(*args, cwd=None):
    # Never echo captured transport failures, which may contain credential material.
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('Trusted command failed: ' + args[0])
    return result.stdout.strip()


def git(*args, cwd=None):
    return run('git', *args, cwd=cwd)


def pair_valid(pair):
    return (isinstance(pair, dict) and set(pair) == set(COMPONENTS)
            and all(isinstance(d, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', d) for d in pair.values()))


def source_valid(sha):
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('Full application SHA required')


def current_main(repo):
    return git('ls-remote', '--exit-code', repo, 'refs/heads/main').split()[0]


def set_images(path, sha, digests):
    text = path.read_text()
    for name in COMPONENTS:
        pattern = rf'(  newName: {re.escape(REGISTRY + name)}\n)  newTag: [^\n]+(?:\n  digest: [^\n]+)?'
        text, count = re.subn(pattern, rf'\g<1>  newTag: sha-{sha}\n  digest: {digests[name]}', text)
        if count != 1:
            raise RuntimeError('Expected exactly one image entry per component')
    path.write_text(text)


def accepted(record, sha, digests):
    """Only observer evidence, not a desired-state promotion, qualifies for PROD."""
    return (record.get('version') == 1 and record.get('status') == 'accepted'
            and record.get('sha') == sha and record.get('digests') == digests
            and pair_valid(digests) and bool(re.fullmatch(r'[0-9a-f]{40}', record.get('infrastructure_commit', '')))
            and bool(re.fullmatch(r'[0-9a-f]{40}', record.get('argo_revision', '')))
            and record.get('checks') == {'argo': True, 'running_images': True, 'url': True})


@contextmanager
def serialized():
    # Jenkins lock spans publication, promotion and acceptance. Also protect local CLI invocations.
    with open(Path.home() / '.liora-stage-promotion.lock', 'a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def commit(directory, environment, sha, allowed):
    existing = {p for p in allowed if (Path(directory) / p).exists()}
    tracked = set(git('ls-files', '--', *sorted(allowed), cwd=directory).splitlines())
    git('add', '--all', '--', *sorted(existing | tracked), cwd=directory)
    paths = set(git('diff', '--cached', '--name-only', cwd=directory).splitlines())
    if not paths <= allowed:
        raise RuntimeError('Writer attempted to modify a forbidden path')
    if not paths:
        return False
    git('-c', 'user.name=Jenkins GitOps', '-c', 'user.email=jenkins-gitops@users.noreply.github.com',
        'commit', '-m', f'chore(gitops): update {environment} for {sha[:12]}',
        '-m', 'Record the verified immutable application image pair and its deployment evidence. '
              'Only the selected environment files are changed; unrelated infrastructure changes are preserved.', cwd=directory)
    return True


def push_main(directory):
    result = subprocess.run(['git', 'push', 'origin', 'HEAD:refs/heads/main'], cwd=directory,
                            text=True, capture_output=True)
    if result.returncode == 0:
        return True
    if 'fetch first' in result.stderr or 'non-fast-forward' in result.stderr:
        return False
    raise RuntimeError('Infrastructure push rejected; verify GitHub App access and branch rules')


def prepare(directory):
    git('fetch', '--quiet', 'origin', 'main', cwd=directory)
    # This is a disposable data checkout, never the operator repository.
    git('reset', '--hard', 'origin/main', cwd=directory)


def promote(environment, sha, digests, release='', app_repo=APPLICATION_REPO,
            infra_repo=INFRASTRUCTURE_REPO, validator=None):
    source_valid(sha)
    if environment == 'prod' and not PROD_ENABLED:
        raise RuntimeError('PROD is blocked until STAGE acceptance is implemented and live-verified')
    if not pair_valid(digests):
        raise ValueError('Both immutable image digests are required')
    if environment not in ('dev', 'stage', 'prod'):
        raise ValueError('Unknown environment')
    if environment == 'prod' and not re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', release):
        raise ValueError('PROD requires vX.Y.Z release tag')
    with serialized(), tempfile.TemporaryDirectory(prefix='liora-gitops-') as directory:
        app = Path(directory) / 'application'
        infra = Path(directory) / 'infrastructure'
        git('clone', '--quiet', '--no-checkout', app_repo, str(app))
        # Validate source membership using application history alone.
        git('cat-file', '-e', sha + '^{commit}', cwd=app)
        if environment != 'dev':
            git('merge-base', '--is-ancestor', sha, 'origin/main', cwd=app)
        if environment == 'prod':
            git('fetch', '--quiet', 'origin', f'refs/tags/{release}:refs/tags/{release}', cwd=app)
            if git('rev-parse', release + '^{commit}', cwd=app) != sha:
                raise RuntimeError('Release tag does not identify the selected application SHA')
        git('clone', '--quiet', '--branch', 'main', infra_repo, str(infra))
        for attempt in range(5):
            prepare(infra)
            overlay = infra / OVERLAYS / environment
            if environment == 'stage' and current_main(app_repo) != sha:
                return {'status': 'superseded', 'sha': sha, 'digests': digests}
            if environment == 'prod':
                found = False
                receipt = f'{OVERLAYS}/stage/acceptance.json'
                for revision in git('log', '--format=%H', 'origin/main', '--', receipt, cwd=infra).splitlines():
                    content = subprocess.run(['git', 'show', f'{revision}:{receipt}'], cwd=infra,
                                             text=True, capture_output=True)
                    if content.returncode == 0 and accepted(json.loads(content.stdout), sha, digests):
                        found = True
                        break
                if not found:
                    raise RuntimeError('PROD requires successful STAGE acceptance of this exact source and pair')
            previous = overlay / 'promotion.json'
            record = {'sha': sha, 'digests': digests, 'release': release}
            unchanged = previous.exists() and json.loads(previous.read_text()) == record
            set_images(overlay / 'kustomization.yaml', sha, digests)
            previous.write_text(json.dumps(record, indent=2) + '\n')
            if environment == 'stage' and not unchanged:
                (overlay / 'acceptance.json').unlink(missing_ok=True)
            run('sh', str(validator or Path(__file__).with_name('validate.sh')), cwd=infra)
            allowed = STAGE_FILES if environment == 'stage' else {
                f'{OVERLAYS}/{environment}/kustomization.yaml', f'{OVERLAYS}/{environment}/promotion.json'}
            changed = commit(infra, environment, sha, allowed)
            head = git('rev-parse', 'HEAD', cwd=infra)
            if environment in ('stage', 'dev'):
                # STAGE follows application main: mandatory recheck immediately before EVERY
                # attempted push (two repositories cannot share an atomic transaction). DEV is a
                # deliberate manual deployment of any branch, so it is never superseded.
                if environment == 'stage' and current_main(app_repo) != sha:
                    return {'status': 'superseded', 'sha': sha, 'digests': digests}
                if changed and not push_main(infra):
                    continue
                return {'status': 'promoted', 'sha': sha, 'digests': digests,
                        'infrastructure_commit': head}
            branch = f'promotion/{environment}/{release or sha}'
            refs = git('ls-remote', 'origin', f'refs/heads/{branch}', cwd=infra)
            if refs:
                git('fetch', '--quiet', 'origin', branch, cwd=infra)
                for path in allowed:
                    if git('show', f'FETCH_HEAD:{path}', cwd=infra) != (infra / path).read_text().strip():
                        raise RuntimeError('Existing review branch differs; manual resolution required')
            elif changed:
                git('push', 'origin', f'HEAD:refs/heads/{branch}', cwd=infra)
            return {'status': 'review-required', 'sha': sha, 'digests': digests,
                    'branch': branch, 'review_url': INFRASTRUCTURE_REPO.removesuffix('.git') + '/compare/main...' + branch}
        raise RuntimeError('Infrastructure main advanced repeatedly; retry manually')


def record_acceptance(record, infra_repo=INFRASTRUCTURE_REPO):
    sha, digests = record['sha'], record['digests']
    source_valid(sha)
    if record.get('status') not in ('accepted', 'failed') or not pair_valid(digests):
        raise ValueError('Invalid observer receipt')
    if record['status'] == 'accepted' and not accepted(record, sha, digests):
        raise ValueError('Incomplete acceptance evidence')
    with serialized(), tempfile.TemporaryDirectory(prefix='liora-acceptance-') as directory:
        git('clone', '--quiet', '--branch', 'main', infra_repo, directory)
        for attempt in range(5):
            prepare(directory)
            stage = Path(directory) / OVERLAYS / 'stage'
            promotion = json.loads((stage / 'promotion.json').read_text())
            if promotion.get('sha') != sha or promotion.get('digests') != digests:
                raise RuntimeError('STAGE has changed; refuse to attach obsolete acceptance')
            git('merge-base', '--is-ancestor', record['infrastructure_commit'], 'origin/main', cwd=directory)
            if record['status'] == 'accepted':
                git('merge-base', '--is-ancestor', record['infrastructure_commit'], record['argo_revision'], cwd=directory)
                git('merge-base', '--is-ancestor', record['argo_revision'], 'origin/main', cwd=directory)
                observed = json.loads(git('show', f"{record['argo_revision']}:{OVERLAYS}/stage/promotion.json", cwd=directory))
                if observed.get('sha') != sha or observed.get('digests') != digests:
                    raise RuntimeError('Observed Argo revision does not contain the selected pair')
            receipt = stage / 'acceptance.json'
            receipt.write_text(json.dumps(record, indent=2) + '\n')
            allowed = {f'{OVERLAYS}/stage/acceptance.json'}
            if not commit(directory, 'stage', sha, allowed) or push_main(directory):
                return
        raise RuntimeError('Infrastructure main advanced repeatedly while recording acceptance')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('environment', choices=['dev', 'stage', 'prod', 'accept-stage'])
    p.add_argument('source', help='Application SHA, or observer receipt for accept-stage')
    p.add_argument('--release', default='')
    p.add_argument('--output', default='stage-request.json')
    args = p.parse_args()
    if args.environment == 'accept-stage':
        record_acceptance(json.loads(Path(args.source).read_text()))
        return
    if args.environment == 'prod' and not PROD_ENABLED:
        raise RuntimeError('PROD is blocked until STAGE acceptance is implemented and live-verified')
    digests = {n: run('crane', 'digest', f'{REGISTRY}{n}:sha-{args.source}') for n in COMPONENTS}
    result = promote(args.environment, args.source, digests, args.release)
    Path(args.output).write_text(json.dumps(result, indent=2) + '\n')
    print('Promotion result:', result['status'])


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        raise SystemExit('Promotion failed (' + type(error).__name__ + '); no credentials retained')
