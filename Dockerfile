# Firmity report backend — FastAPI + matplotlib/fpdf report rendering.
# Slim Python base; matplotlib/Pillow/fpdf2 install from manylinux wheels (no apt libs needed).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first (better layer caching)
COPY requirements.txt requirements-report.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-report.txt \
    && pip install --no-cache-dir "gunicorn==23.0.0"

# App code (includes app/assets/fonts/WorkSans-*.ttf used by the report)
COPY app ./app

EXPOSE 8000

# 2 gunicorn workers with the uvicorn ASGI worker. Report rendering is CPU-bound,
# so keep workers modest; rely on async for I/O. $PORT is honoured if the platform sets it.
CMD ["sh", "-c", "gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w 2 -b 0.0.0.0:${PORT:-8000} --timeout 90"]
