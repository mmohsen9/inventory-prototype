# Dockerfile for CranL and Container Deployments
FROM python:3.11-slim

# Prevent Python from writing pyc files and keep stdout unbuffered
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# Install system dependencies (for building packages if needed) + headless browser & fonts for PDF vouchers
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    chromium \
    fonts-liberation \
    fonts-noto-core \
    fonts-kacst \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Ensure upload and instance directories exist
RUN mkdir -p uploads instance /var/data

# Run database indexes / prep step
RUN python migrate_core_indexes.py || true

EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/health || exit 1

# Start Gunicorn
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 4 --timeout 120"]

