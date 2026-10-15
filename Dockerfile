# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Non-root user that may still bind ports 80 and 502 (CAP_NET_BIND_SERVICE on the interpreter).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libcap2-bin \
    && rm -rf /var/lib/apt/lists/* \
    && setcap cap_net_bind_service=+ep /usr/local/bin/python3.12 \
    && useradd --system --uid 1000 --home-dir /app --shell /usr/sbin/nologin shelly \
    && mkdir -p /app /config \
    && chown shelly:shelly /app /config

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY shelly_emu ./shelly_emu
RUN pip install --no-cache-dir --root-user-action=ignore . \
    && rm -rf /root/.cache

COPY config.example.yaml /config/config.example.yaml

USER shelly
ENV CONFIG=/config/config.yaml \
    PYTHONUNBUFFERED=1

# Sigen talks to port 80 (HTTP RPC) and, for pro3em, 502 (Modbus TCP). mDNS uses UDP 5353.
# These are only reachable with network_mode: host or a macvlan network, see README.
EXPOSE 80/tcp 502/tcp 5353/udp

ENTRYPOINT ["shelly-emu"]
