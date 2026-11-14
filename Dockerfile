FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

# Install system dependencies including FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser
RUN python -m playwright install chromium

# Copy application source code
COPY app/ ./app/
COPY run.py .

# Create volume/storage mount points
RUN mkdir -p downloads data/cookies

# Default configuration for container runtime
ENV UVD_HOST=0.0.0.0
ENV PORT=10000
ENV PYTHONUNBUFFERED=1

EXPOSE 8000
EXPOSE 10000

CMD ["python", "run.py"]
