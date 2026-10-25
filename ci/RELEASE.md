# PROD release and rollback

PROD never builds anything. A release promotes the exact image pair that STAGE already ran and
accepted, under a `vX.Y.Z` tag, through a reviewed pull request in the infrastructure repository.
Argo CD on the PROD cluster deploys the merged change (`app-prod`: automated, self-heal, no prune).

## Release

1. Pick a candidate: an application main SHA whose STAGE acceptance is `accepted`.
   In the infrastructure repository:

   ```bash
   git log --format='%h %s' -- kubernetes/apps/liora/overlays/stage/acceptance.json
   git show <commit>:kubernetes/apps/liora/overlays/stage/acceptance.json   # status, sha, digests
   ```

2. Tag that exact SHA in the application repository and push the tag:

   ```bash
   git tag -a v1.0.0 <sha> -m "Release v1.0.0"
   git push origin v1.0.0
   ```

3. Jenkins → `trusted/publish` → **Build with Parameters**: `ACTION=prod`, `APP_SHA=<full sha>`,
   `RELEASE_TAG=v1.0.0` (`SOURCE_JOB` and `CI_BUILD` are ignored). The publisher reads the image
   digests of `sha-<sha>` from GHCR, requires an accepted STAGE receipt for exactly that SHA and
   pair, and pushes the branch `promotion/prod/v1.0.0`. The build description shows the PR link.
4. Open and merge that infrastructure pull request. It changes only
   `kubernetes/apps/liora/overlays/prod/kustomization.yaml` and `promotion.json`.
5. Verify PROD (Argo CD `https://192.168.77.17:8080`): `app-prod` Synced/Healthy on the merge
   commit, both backend and frontend Pods run the digests from `promotion.json`, the prestart
   Job completed, `https://liora.aprt.info` loads and `/api/v1/utils/health-check/` returns `true`.

## Rollback

Preferred: release the previous version again with the same procedure, using its existing tag and
SHA (for example `ACTION=prod`, `APP_SHA=<v1.0.0 sha>`, `RELEASE_TAG=v1.0.0`). Its acceptance
receipt is still in the infrastructure history, so the publisher allows it; nothing is rebuilt.
Because `promotion/prod/v1.0.0` was already merged, the publisher creates a fresh review branch
`promotion/prod/v1.0.0-redeploy-<infra sha>`; merge its PR. An unmerged branch with the same
content is reused, and a release that PROD already runs reports `unchanged` (nothing to merge).

Fallback when Jenkins is unavailable: revert the PROD promotion merge commit in the
infrastructure repository through a pull request; Argo CD deploys the previous pair.

**Database migrations are forward-only.** Before rolling back, check whether Alembic migrations
were added between the two versions (`git diff <old>..<new> -- backend/app/alembic/versions`).
If they were, the older application may not match the newer schema: coordinate a manual
downgrade or roll forward with a fix instead.
