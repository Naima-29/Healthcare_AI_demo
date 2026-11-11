# Core service image. Build from the repo root:  docker build -t kcd-core .
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md ./
COPY core ./core
RUN python -m venv /opt/venv && /opt/venv/bin/pip install ".[service]"

FROM python:3.12-slim
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --system --uid 10001 --home /app kcd
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY contracts/policy_catalog.json contracts/policy_catalog.json
COPY seats/manifests seats/manifests
COPY infra/migrations infra/migrations
USER kcd
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"
CMD ["python", "-m", "kcd_core.service"]
