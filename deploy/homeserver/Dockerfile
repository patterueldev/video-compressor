# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm

# ffmpeg (with libx265) is the only system dependency of the two-pass encode recipe.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the package without the optional GUI extra (the worker never needs PySide6).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Run as an unprivileged user; compose overrides uid/gid to match the archive owner.
RUN useradd --create-home --uid 1000 worker
USER worker

ENTRYPOINT ["vc", "worker"]
CMD ["--config", "/config/worker.toml"]
