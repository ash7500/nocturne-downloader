FROM python:3.12-slim

# Install ffmpeg for audio/video merging
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY app/ ./app/

# Create downloads directory
RUN mkdir -p /app/downloads

ENV DOWNLOADS_DIR=/app/downloads
ENV PORT=8000

EXPOSE 8000

# Render injects PORT; fall back to 8000 locally.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips=*"]
