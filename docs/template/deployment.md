# Application deployment

## Ownership

Application source, tests, image builds and trusted publication helpers live in
this repository. Infrastructure and GitOps manifests live in the
[infrastructure repository](https://github.com/fastapi-traefik-devops/fastapi-traefik-liora-infrastructure).

## Operator procedures

- [Fresh deployment and operating sequence](https://github.com/fastapi-traefik-devops/fastapi-traefik-liora-infrastructure/blob/main/README.md).
- [Deploy application manifests](https://github.com/fastapi-traefik-devops/fastapi-traefik-liora-infrastructure/blob/main/runbooks/13-deploy-application-manifests.md).
- [Jenkins configuration](https://github.com/fastapi-traefik-devops/fastapi-traefik-liora-infrastructure/blob/main/runbooks/17-migrate-jenkins-ci.md).
- [CI profiles](../../ci/README.md) and [publication](../../ci/PUBLICATION.md).
- [Manual PROD release and rollback](../../ci/RELEASE.md).

STAGE follows successful main CI and immutable publication. DEV remains manual;
PROD reuses the accepted STAGE digest pair through its release procedure.
Do not apply manifests from historical project material.

For local application development, use [the development guide](development.md).
The former kind/Docker-host deployment procedure is preserved in the
[infrastructure migration archive](https://github.com/fastapi-traefik-devops/fastapi-traefik-liora-infrastructure/blob/main/docs/testreports/application-legacy-archive/README.md).
