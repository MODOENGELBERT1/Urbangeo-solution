# UrbanSanity v13 — Single-container Dockerfile for Railway/Render/Fly.io
# FastAPI serves both the API and the frontend static files
FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy FastAPI backend
COPY backend/main.py .

# Copy frontend static files (served by FastAPI at runtime)
COPY frontend/ ./frontend/

# Railway injects PORT env var; default to 8080 for local testing
ENV PORT=8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD curl -f http://localhost:${PORT}/health || exit 1

# Start uvicorn — uses $PORT from Railway
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
