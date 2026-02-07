#!/bin/bash
set -e

echo "========================================"
echo "DirecTV Stream EPG Container Starting"
echo "========================================"

# Check for .env file
if [ ! -f /app/.env ]; then
    echo "WARNING: .env file not found!"
    echo "Checking for environment variables..."
fi

# Check credentials from env vars (Portainer-friendly)
if [ -z "$DTV_USERNAME" ] || [ -z "$DTV_PASSWORD" ]; then
    # Try loading from .env if it exists
    if [ -f /app/.env ]; then
        source /app/.env
    fi
fi

# Final validation
if [ -z "$DTV_USERNAME" ] || [ -z "$DTV_PASSWORD" ]; then
    echo "ERROR: DTV_USERNAME and DTV_PASSWORD must be set!"
    echo ""
    echo "Set via environment variables (Portainer) OR create /app/.env:"
    echo "  DTV_USERNAME=your-email@example.com"
    echo "  DTV_PASSWORD=your-password"
    exit 1
fi

echo "✓ Credentials loaded"

# Ensure directories exist
mkdir -p /app/data /app/out /var/log/directv

# Set permissions
chmod 755 /app/data /app/out /var/log/directv

echo "✓ Directories ready"

# Get configuration
WEB_PORT=${WEB_PORT:-8675}
REFRESH_HOUR=${REFRESH_HOUR:-3}
REFRESH_MINUTE=${REFRESH_MINUTE:-0}
TZ=${TZ:-America/New_York}

echo "✓ Configuration:"
echo "  - Web Port: $WEB_PORT"
echo "  - Refresh Time: ${REFRESH_HOUR}:${REFRESH_MINUTE} $TZ"
echo "  - Timezone: $TZ"
echo "========================================"
echo "Starting web server..."
echo "Admin UI: http://localhost:$WEB_PORT/"
echo "EPG URL: http://localhost:$WEB_PORT/files/dtv_epg.xml"
echo "M3U URL: http://localhost:$WEB_PORT/files/dtv_channels.m3u"
echo "========================================"

# Start Flask webapp (includes scheduler and file server)
exec python /app/webapp.py
