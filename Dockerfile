# API image: FastAPI + uvicorn. The map is served separately as a static site.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY floodrules ./floodrules
RUN pip install .

# Verified insurer rules (data/rules/*.json) ship with the image; /coverage and /property read them.
COPY data/rules ./data/rules
ENV FLOODRULES_RULES_DIR=/app/data/rules
# /extract stays disabled until FLOODRULES_ADMIN_TOKEN is set, so a missing token can't leave it open
ENV FLOODRULES_REQUIRE_ADMIN=1

RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

ENV PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=4)"
# Proxy headers are handled by the app (FLOODRULES_TRUST_PROXY), not uvicorn, so they can't be trusted by accident.
# Access logs come from the app's JSON request log instead of uvicorn's.
CMD ["sh", "-c", "exec uvicorn floodrules.api:app --host 0.0.0.0 --port ${PORT} --no-proxy-headers --no-access-log"]
