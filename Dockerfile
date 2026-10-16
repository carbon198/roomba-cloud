# Compile against the runtime OpenSSL family; no compiler/dev quality tools in runtime.
FROM python:3.14-slim-bookworm AS builder
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY native/mqtt_tls_bridge.c .
RUN gcc -O2 -Wall -Wextra -Werror mqtt_tls_bridge.c -o mqtt_tls_bridge -lssl -lcrypto

FROM python:3.14-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends openssl libssl3 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 roomba && useradd --uid 10001 --gid roomba --no-create-home roomba \
    && install -d -m 700 -o roomba -g roomba /data \
    && install -d -m 755 -o roomba -g roomba /data/dns-challenges
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Keep COPY sources and the .dockerignore allowlist aligned (test_deployment checks this).
# Never add private data, captured maps, firmware, credentials or deployment files.
COPY roomba_cloud/ roomba_cloud/
COPY deployment/ deployment/
COPY --from=builder /build/mqtt_tls_bridge /app/tools/mqtt_tls_bridge
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Host-mounted PEM paths must be traversable/readable by this fixed non-root UID.
USER 10001:10001
ENTRYPOINT ["python", "-m", "deployment.docker_entrypoint"]
CMD ["both"]
