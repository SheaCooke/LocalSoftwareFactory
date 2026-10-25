# Sandbox image for shell.run_sandboxed. Build with `factory sandbox build`.
FROM python:3.12-slim

RUN pip install --no-cache-dir "pytest>=8" "ruff>=0.6" "mypy>=1.11" \
 && useradd --uid 1000 --create-home --shell /bin/sh sandbox

USER 1000:1000
WORKDIR /workspace
