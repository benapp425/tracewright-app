# Tracewright on a server: KiCad's official image (kicad-cli, and its Python with pcbnew), the app,
# git, and Java for the optional Freerouting autorouter. See DEPLOY.md.
#
#   docker build -t tracewright .
#   docker run -p 127.0.0.1:8764:8764 -v tracewright-data:/data --env-file deploy/.env tracewright
#
# KICAD_IMAGE: 10.0-full carries the 3D models (renders show parts); 10.0 is smaller, renders are bare.
ARG KICAD_IMAGE=kicad/kicad:10.0-full
FROM ${KICAD_IMAGE}

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-venv python3-pip git ca-certificates curl tini \
      openjdk-17-jre-headless \
 && rm -rf /var/lib/apt/lists/* \
 && (id -u kicad >/dev/null 2>&1 || useradd -m -u 1000 kicad)

# Freerouting (GPL-3.0, optional): fetched at build time, run headless with its analytics off
ARG FREEROUTING_VERSION=2.1.0
RUN curl -fsSL -o /opt/freerouting.jar \
      "https://github.com/freerouting/freerouting/releases/download/v${FREEROUTING_VERSION}/freerouting-${FREEROUTING_VERSION}.jar" \
    || echo "Freerouting not downloaded: the built-in router still works"

COPY . /opt/tracewright-src
RUN python3 -m venv /opt/tw \
 && /opt/tw/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/tw/bin/pip install --no-cache-dir /opt/tracewright-src \
 && rm -rf /opt/tracewright-src \
 && mkdir -p /data && chown -R kicad /data

ENV TRACEWRIGHT_HOME=/data/app \
    TW_WORKSPACE=/data/projects \
    TW_SERVER_MODE=1 \
    TW_KICAD_CLI=/usr/bin/kicad-cli \
    TW_KICAD_PYTHON=/usr/bin/python3 \
    TW_FREEROUTING=/opt/freerouting.jar \
    PATH=/opt/tw/bin:$PATH

USER kicad
WORKDIR /data
VOLUME /data
EXPOSE 8764
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s CMD curl -fsS http://127.0.0.1:8764/api/health || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
# a password (TW_PASSWORD) is required: the server refuses to listen beyond 127.0.0.1 without one
CMD ["tracewright", "serve", "--host", "0.0.0.0", "--port", "8764", "--no-browser", "--server"]
