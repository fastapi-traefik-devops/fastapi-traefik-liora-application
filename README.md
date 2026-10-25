# Liora

Liora is a full-stack web application (FastAPI backend, React frontend, PostgreSQL) with user
accounts, JWT authentication and item management, delivered through a self-hosted DevSecOps
pipeline: Jenkins → GHCR → GitOps → Argo CD on two private K3s clusters.

This repository holds the **application**: source code, tests, Dockerfiles and the Jenkins
pipelines. Clusters, deployment manifests, databases, monitoring and recovery live in the
[infrastructure repository](https://github.com/fastapi-traefik-devops/fastapi-traefik-liora-infrastructure).

Based on [full-stack-fastapi-template](https://github.com/fastapi/full-stack-fastapi-template).

## Environments

| Environment | URL | Cluster | How it is deployed |
| --- | --- | --- | --- |
| DEV | https://dev.liora.aprt.info | NONPROD K3s | manually, any branch (`dev-build` job) |
| STAGE | https://stage.liora.aprt.info | NONPROD K3s | automatically on every merge to `main`, with automatic acceptance |
| PROD | https://liora.aprt.info | PROD K3s | manually, by release tag `vX.Y.Z` through a reviewed infrastructure PR |

## Where to find what

| Topic | Location |
| --- | --- |
| Backend development | [backend/README.md](backend/README.md) |
| Frontend development | [frontend/README.md](frontend/README.md) |
| Local stack with Docker Compose | [docs/template/development.md](docs/template/development.md) |
| CI gates and profiles | [ci/README.md](ci/README.md) |
| Image publication | [ci/PUBLICATION.md](ci/PUBLICATION.md) |
| PROD release and rollback | [ci/RELEASE.md](ci/RELEASE.md) |
| CI/CD overview and setup | [infrastructure `docs/CI_CD.md`](https://github.com/fastapi-traefik-devops/fastapi-traefik-liora-infrastructure/blob/main/docs/CI_CD.md) |
| Cluster installation and operations | [infrastructure README and runbooks](https://github.com/fastapi-traefik-devops/fastapi-traefik-liora-infrastructure/blob/main/README.md) |
| Deployment manifests | infrastructure `kubernetes/apps/liora/overlays/{dev,stage,prod}` |

## CI/CD in short

```text
pull request ── app-ci/PR-<n>, profile full (18 gates) ── GitHub status ── merge
main ────────── app-ci/main, profile release ── trusted publisher ── GHCR + infra commit ── Argo CD ── STAGE ── acceptance.json
any branch ──── dev-build (manual) ── trusted publisher ── infra commit ── Argo CD ── DEV
vX.Y.Z tag ──── trusted/publish ACTION=prod (manual) ── infra PR ── review + merge ── Argo CD ── PROD
nightly ─────── app-ci/main, profile full, no publication
```

- Jenkins runs on the NONPROD K3s cluster and is reachable only through Tailscale. Builds run
  in isolated Docker-in-Docker on `host-ci`, which holds no write credentials.
- Only the trusted `registry-publisher` agent pushes images and commits to the infrastructure
  repository; it runs commit-pinned helper scripts, never code from a build.
- Security gates: gitleaks, pip-audit, npm audit, Trivy image scans, non-root image smoke tests
  and an attestation (`gates.json`) that the publisher requires.
- Images are tagged with the full commit SHA and deployed by digest. PROD reuses exactly the
  image pair that STAGE accepted; nothing is rebuilt for a release. Roll back by re-releasing
  the previous tag.

## Technology stack

- [FastAPI](https://fastapi.tiangolo.com) backend with [SQLModel](https://sqlmodel.tiangolo.com),
  [Pydantic](https://docs.pydantic.dev) and [PostgreSQL](https://www.postgresql.org)
  (CloudNativePG on the clusters); Alembic migrations; tests with [Pytest](https://pytest.org).
- [React](https://react.dev) frontend with TypeScript, Vite, [Chakra UI](https://chakra-ui.com),
  a generated API client and [Playwright](https://playwright.dev) end-to-end tests.
- Secure password hashing, JWT authentication, email-based password recovery.
- Delivery: Jenkins, GitHub Container Registry, Argo CD, K3s, Traefik ingress, cert-manager.
- Docker Compose for local development only.

## Screenshots

### Login

![Login](img/login.png)

### Dashboard - Admin

![Dashboard](img/dashboard.png)

### Dashboard - Create User

![Create user](img/dashboard-create.png)

### Dashboard - Items

![Items](img/dashboard-items.png)

### Dashboard - User Settings

![User settings](img/dashboard-user-settings.png)

### Dashboard - Dark Mode

![Dark mode](img/dashboard-dark.png)

### Interactive API Documentation

![API docs](img/docs.png)

## Local development

Start the local stack with Docker Compose; details in
[docs/template/development.md](docs/template/development.md):

```bash
docker compose watch
```

The `.env` file holds local development values only. Replace every `changethis` value before
using the stack anywhere but your own machine, for example with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Cluster secrets are not taken from `.env`: they are created by the bootstrap procedures of the
infrastructure repository.

Historical project material remains under `dev-journal/` and `docs/template/archive/`; it is
not the current deployment procedure.
