# One image for every T6 runtime role. The role is the container command:
#   python -m t6.server    (VPS)
#   python -m t6.worker    (Mac mini, main and Frame.io workers)
# The default command starts the server. Build for amd64 only.
FROM python:3.12-slim-bookworm AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/t6 \
 && /opt/t6/bin/pip install --no-cache-dir ".[server,worker,notion,privacy,llm,frameio]"

FROM python:3.12-slim-bookworm
ENV PATH=/opt/t6/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    T6_QUEUE_DB=/data/t6-queue.sqlite \
    T6_REGISTRY_DB=/data/t6-registry.sqlite
COPY --from=build /opt/t6 /opt/t6
RUN useradd --system --uid 10001 --no-create-home t6 \
 && mkdir /data && chown t6 /data
USER 10001
WORKDIR /data
VOLUME /data
EXPOSE 8080 8081
CMD ["python", "-m", "t6.server"]
