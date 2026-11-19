# The Agreement Desk: web app, JSON API and webhooks in one container.
# Build:  docker build -t acme-agreement-desk .
#         Behind a TLS-inspecting proxy, hand it the proxy's CA bundle (never baked into the image):
#         docker build --secret id=ca_bundle,src=/path/to/ca-bundle.crt -t acme-agreement-desk .
# Run:    docker compose up   (settings in .env, see .env.example)

FROM node:22-bookworm-slim AS node

FROM python:3.11-slim-bookworm

# The `claude` CLI, which is how the agents call the model (it needs Node 22).
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca_bundle; fi \
    && ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g @anthropic-ai/claude-code@2.1.308 \
    && npm cache clean --force

WORKDIR /app
COPY requirements.txt .
# wordllama has no ARM (aarch64) wheel, so pip compiles it there. Install g++
# for this step only and remove it in the same layer to keep the image small.
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi \
    && apt-get update && apt-get install -y --no-install-recommends g++ \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove g++ && rm -rf /var/lib/apt/lists/*
COPY src src
COPY data data

# Run as an ordinary user; agreements, versions and signed PDFs live on the /data volume.
RUN useradd --create-home --uid 10001 desk && mkdir /data && chown desk /data
USER desk
ENV DESK_DATA_DIR=/data PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
CMD ["python", "src/main.py", "serve", "--host", "0.0.0.0", "--port", "8000"]
