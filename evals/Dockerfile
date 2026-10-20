FROM python:3.12.12-slim-bookworm
RUN pip install --no-cache-dir uv==0.9.5 && apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
ENV UV_PYTHON_INSTALL_DIR=/opt/pythons
COPY . /opt/rio-source
RUN cd /opt/rio-source && UV_PROJECT_ENVIRONMENT=/opt/rio uv sync --frozen --no-dev
ENV PATH=/opt/rio/bin:/usr/local/bin:/usr/bin:/bin
WORKDIR /workspace
