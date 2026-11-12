# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv
FROM python:3.13.16-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS builder

COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-group deploy --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-group deploy --no-editable

FROM python:3.13.16-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS runtime

ARG APP_REVISION=dev
LABEL org.opencontainers.image.title="Fullish" \
    org.opencontainers.image.description="QQ group companion deployment foundation" \
    org.opencontainers.image.revision=$APP_REVISION
ENV APP_REVISION=$APP_REVISION \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --gid 10001 fullish \
    && useradd --uid 10001 --gid fullish --no-create-home --home-dir /tmp fullish \
    && install -d -m 700 -o 10001 -g 10001 /var/lib/fullish/tool-audit \
    && install -d -m 750 -o 10001 -g 10001 /var/lib/fullish/control \
    && install -d -m 700 -o 10001 -g 10001 /var/lib/fullish/wallet
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
USER 10001:10001
EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/health/ready', timeout=2).close()"]
CMD ["python", "-m", "fullish"]

FROM node:24.13.0-bookworm-slim@sha256:4660b1ca8b28d6d1906fd644abe34b2ed81d15434d26d845ef0aced307cf4b6f AS unified-deps
WORKDIR /opt/fullish-agent
COPY deploy/agent/package.json deploy/agent/package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev
WORKDIR /opt/fullish-plugin
COPY plugins/visualize/package.json plugins/visualize/package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev
WORKDIR /opt/fullish-plugin/renderer
COPY plugins/visualize/renderer/package.json plugins/visualize/renderer/package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev

FROM python:3.13.16-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS unified-system
ARG CHROMIUM_VERSION=156.0.8078.79-1~deb12u1
ARG NOTO_CJK_VERSION=1:20220127+repack1-1
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 -o Acquire::https::Timeout=30 update \
    && apt-get -o Acquire::Retries=3 -o Acquire::https::Timeout=30 install --no-install-recommends -y \
        "chromium=$CHROMIUM_VERSION" "fonts-noto-cjk=$NOTO_CJK_VERSION" \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 fullish \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp fullish \
    && groupadd --gid 10003 fullish-ipc \
    && useradd --uid 10002 --gid 10003 --no-create-home fullish-dim \
    && install -d -m 700 -o 10001 -g 10001 /var/lib/fullish/tool-audit /var/lib/fullish/wallet \
    && install -d -m 750 -o 10001 -g 10001 /var/lib/fullish/control \
    && install -d -m 700 -o 10002 -g 10003 /var/lib/fullish-integrated-agent \
    && install -d -m 2770 -o 10002 -g 10003 /var/lib/fullish-integrated-work \
    && install -d -m 2750 -o 10001 -g 10003 /var/lib/fullish-diagnostics \
    && install -d -m 755 /run/fullish-integrated \
    && install -d -m 2750 -o 10001 -g 10003 /run/fullish-integrated/fullish \
    && install -d -m 2750 -o 10002 -g 10003 /run/fullish-integrated/dim

FROM unified-system AS unified
ARG APP_REVISION=dev
LABEL org.fullish.unified-runtime="true" \
    org.opencontainers.image.title="Fullish" \
    org.opencontainers.image.revision=$APP_REVISION
ENV APP_REVISION=$APP_REVISION \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 PORT=8080 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=unified-deps /usr/local/bin/node /usr/local/bin/node
COPY --from=unified-deps /opt/fullish-agent /opt/fullish-agent
COPY --from=unified-deps /opt/fullish-plugin /opt/fullish-plugin
COPY deploy/agent/launch.mjs deploy/agent/control.mjs /opt/fullish-agent/
COPY deploy/unified /opt/fullish-integrated
COPY plugins/visualize/index.ts plugins/visualize/render-service.mjs plugins/visualize/inspect.mjs plugins/visualize/dim-plugin.json plugins/visualize/defaults.json /opt/fullish-plugin/
COPY plugins/visualize/renderer/render.mjs /opt/fullish-plugin/renderer/render.mjs
COPY plugins/visualize/guides /opt/fullish-plugin/guides
COPY plugins/visualize/skills /opt/fullish-plugin/skills
COPY src /opt/fullish-readonly/src
COPY docs /opt/fullish-readonly/docs
COPY README.md pyproject.toml /opt/fullish-readonly/
USER 10001:10001
EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/health/ready', timeout=2).close()"]
CMD ["python", "-m", "fullish"]

# Plain docker build remains the slim, single-process foundation image.
FROM runtime AS default
