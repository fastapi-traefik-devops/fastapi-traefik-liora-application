FROM node:26.12.0-bookworm-slim@sha256:742a0fe7320e09e0321222b68165e6ac731d30470e7ea7f98f9bfc3a6e1034fe AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit
COPY . ./
ARG VITE_API_URL=""
ENV VITE_API_URL=${VITE_API_URL}
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.30.6-alpine@sha256:f5116ed4606f3eb015dd4a8e28cb9b351476dc0ad4bc6e9f93b19cec130cdf03 AS runtime
# Apply Alpine security fixes that the pinned base image does not have yet
# (CVE-2026-77214, libexpat 2.8.5 -> 2.9.0). Nginx itself is not touched.
USER root
RUN apk upgrade --no-cache
COPY --from=build --chown=101:101 /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY nginx-backend-not-found.conf /etc/nginx/extra-conf.d/backend-not-found.conf
USER 101:101
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1/
