# Single-stage: slim base, no build toolchain in the final image.
FROM python:3.13-slim

# TZ sets when the daily spend caps reset (local midnight); override with -e TZ=...
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC \
    SWITCHBOARD_CONFIG=/config/switchboard.yml

RUN useradd --system --uid 10001 --no-create-home switchboard \
 && mkdir -p /state && chown switchboard /state

WORKDIR /app

# Dependencies first, so a source edit does not invalidate the install layer.
COPY pyproject.toml README.md LICENSE ./
COPY switchboard/__init__.py ./switchboard/
RUN pip install --no-cache-dir .

COPY switchboard/ ./switchboard/
RUN pip install --no-cache-dir --no-deps .

USER switchboard

EXPOSE 8000

# /health is the one unauthenticated route; it only proves the process is up.
HEALTHCHECK --interval=1m --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

ENTRYPOINT ["switchboard"]
CMD ["serve"]
