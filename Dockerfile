# syntax=docker/dockerfile:1.7
ARG PLANKTON_RUNTIME_BASE=plankton-inference-runtime:replace-with-immutable-base
ARG PLANKTON_RUNTIME_BASE_ID=sha256:unknown
ARG PLANKTON_SOURCE_COMMIT=unknown
FROM node:24-alpine AS web

WORKDIR /build
COPY apps/web/package.json apps/web/package-lock.json ./apps/web/
RUN --mount=type=cache,target=/root/.npm npm --prefix apps/web ci
COPY apps/web ./apps/web
RUN npm --prefix apps/web run build

FROM ${PLANKTON_RUNTIME_BASE}

ARG PLANKTON_RUNTIME_BASE
ARG PLANKTON_RUNTIME_BASE_ID
ARG PLANKTON_SOURCE_COMMIT
LABEL org.opencontainers.image.revision=${PLANKTON_SOURCE_COMMIT} \
      org.plankton.runtime-base=${PLANKTON_RUNTIME_BASE} \
      org.plankton.runtime-base-id=${PLANKTON_RUNTIME_BASE_ID}

USER root
WORKDIR /opt/plankton-inference

COPY pyproject.toml ./
COPY apps/api ./apps/api
COPY apps/worker ./apps/worker
COPY packages ./packages
COPY host/usb_agent ./host/usb_agent
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --no-deps --no-build-isolation .

COPY --from=web --chown=plankton:plankton /build/apps/web/dist ./apps/web/dist
USER 10001:10001

CMD ["python", "-m", "plankton_api"]
