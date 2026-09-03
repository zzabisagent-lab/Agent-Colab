# Agent-Colab Secret sidecar (development plan §9.4). Run read-only with a tmpfs runtime dir:
#   docker run --read-only --tmpfs /run/agent-colab-sidecar:rw,noexec,nosuid,size=1m \
#     -e AGENT_COLAB_SIDECAR_BROKER_URL=https://colab.example/ \
#     -e AGENT_COLAB_SIDECAR_TOKEN_FILE=/run/secrets/sidecar-token \
#     agent-colab-sidecar run --handle sh-... --mode fd -- /usr/local/bin/agent
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY agent_colab_sidecar ./agent_colab_sidecar
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    AGENT_COLAB_SIDECAR_RUNTIME_DIR=/run/agent-colab-sidecar
COPY --from=build /install /usr/local
RUN groupadd --system sidecar && useradd --system --gid sidecar --no-create-home --shell /usr/sbin/nologin sidecar \
    && mkdir -p /run/agent-colab-sidecar && chown sidecar:sidecar /run/agent-colab-sidecar \
    && chmod 0700 /run/agent-colab-sidecar
USER sidecar
VOLUME ["/run/agent-colab-sidecar"]
ENTRYPOINT ["agent-colab-sidecar"]
CMD ["status"]
