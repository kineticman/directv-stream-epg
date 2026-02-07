# DirecTV Stream EPG Docker Container
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    wget \
    curl \
    gnupg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright browsers (Firefox only for headless stability)
RUN pip install --no-cache-dir playwright && \
    playwright install firefox && \
    playwright install-deps firefox

# Set working directory
WORKDIR /app

# Copy application files
COPY *.py /app/
COPY templates/ /app/templates/
COPY .env.example /app/

# Install Python dependencies
RUN pip install --no-cache-dir requests flask apscheduler pytz

# Create directories
RUN mkdir -p /app/data /app/out /var/log/directv

# Create entrypoint script
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Expose web server port (configurable via WEB_PORT env var)
EXPOSE 8675

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${WEB_PORT:-8675}/health || exit 1

# Run entrypoint
ENTRYPOINT ["/docker-entrypoint.sh"]
