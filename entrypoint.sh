#!/bin/bash
set -e

echo "========================================"
echo "DirecTV Stream EPG Container Starting"
echo "========================================"

# Check for .env file (optional - docker-compose env_file is preferred)
if [ -f /app/.env ]; then
    echo "✓ Loading .env file"
    export $(cat /app/.env | grep -v '^#' | xargs)
else
    echo "WARNING: .env file not found!"
fi

# Check for required credentials
echo "Checking for environment variables..."
if [ -z "$DTV_USERNAME" ] && [ -z "$DTV_EMAIL" ]; then
    echo "ERROR: DTV_USERNAME and DTV_PASSWORD must be set!"
    echo ""
    echo "Set via environment variables (Portainer) OR create /app/.env:"
    echo "  DTV_USERNAME=your-email@example.com"
    echo "  DTV_PASSWORD=your-password"
    exit 1
fi

echo "✓ Credentials loaded"

# Create required directories
mkdir -p /app/data /app/out /var/log/directv /app/templates
echo "✓ Directories ready"

# Display configuration
echo "✓ Configuration:"
echo "  - Web Port: ${WEB_PORT:-8675}"
echo "  - Refresh Time: ${REFRESH_HOUR:-3}:${REFRESH_MINUTE:-0} ${TZ:-America/New_York}"
echo "  - Timezone: ${TZ:-America/New_York}"
echo "========================================"

# Start the web server
echo "Starting web server..."
echo "Admin UI: http://localhost:${WEB_PORT:-8675}/"
echo "EPG URL: http://localhost:${WEB_PORT:-8675}/files/dtv_epg.xml"
echo "M3U URL: http://localhost:${WEB_PORT:-8675}/files/dtv_channels.m3u"
echo "========================================"

exec python webapp.py
