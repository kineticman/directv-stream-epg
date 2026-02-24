# DirecTV Stream EPG Server

Automatically generates XMLTV EPG and M3U playlists from your DirecTV Stream subscription, served via a web interface with daily auto-refresh.

## Quick Start (Portainer)

Add a new stack with this compose:

```yaml
version: '3.8'

services:
  directv-epg:
    image: ghcr.io/kineticman/directv-stream-epg:latest
    container_name: directv-epg
    restart: unless-stopped

    ports:
      - "${WEB_PORT:-8675}:${WEB_PORT:-8675}"

    volumes:
      - ./data:/app/data
      - ./out:/app/out
      - ./logs:/var/log/directv

    environment:
      - DTV_USERNAME=${DTV_USERNAME}
      - DTV_PASSWORD=${DTV_PASSWORD}
      - HOST_IP=${HOST_IP}
      - WEB_PORT=${WEB_PORT:-8675}
      - REFRESH_HOUR=${REFRESH_HOUR:-3}
      - REFRESH_MINUTE=${REFRESH_MINUTE:-0}
      - TZ=${TZ:-America/New_York}
      # Optional: PrismCast integration (see below)
      # - PRISMCAST_HOST=${PRISMCAST_HOST}
      # - PRISMCAST_PORT=${PRISMCAST_PORT:-5589}
      # Optional: Renumber channels starting from a specific number
      # - CHNO_START=${CHNO_START:-0}
```

Set these in Portainer's **Environment Variables** section — do not paste credentials directly into the compose file:

| Variable | Required | Description |
|----------|----------|-------------|
| `DTV_USERNAME` | Yes | Your DirecTV Stream email |
| `DTV_PASSWORD` | Yes | Your DirecTV Stream password |
| `HOST_IP` | Yes | Your server's LAN IP (e.g. `192.168.1.100`) |
| `WEB_PORT` | No | Web admin port (default: `8675`) |
| `REFRESH_HOUR` | No | Daily refresh hour 0–23 (default: `3`) |
| `REFRESH_MINUTE` | No | Daily refresh minute (default: `0`) |
| `TZ` | No | Timezone (default: `America/New_York`) |
| `PRISMCAST_HOST` | No | PrismCast server IP — enables PrismCast outputs |
| `PRISMCAST_PORT` | No | PrismCast port (default: `5589`) |
| `CHNO_START` | No | Renumber channels from this value (default: `0` = keep original) |

Then open the admin UI: `http://your-server-ip:8675/`

## Output Files

| File | Description |
|------|-------------|
| `dtv_epg.xml` | XMLTV guide data (2 days) |
| `dtv_channels.m3u` | Standard M3U playlist |
| `prismcast_enriched.m3u` | M3U with PrismCast HLS URLs + EPG tvg-ids *(requires `PRISMCAST_HOST`)* |
| `prismcast_channels.json` | PrismCast channel import file *(requires `PRISMCAST_HOST`)* |

All files are accessible from the admin UI with copy-paste URLs.

## Media Server Setup

**Plex / Jellyfin / Emby / Channels DVR**
- M3U: `http://your-server-ip:8675/files/dtv_channels.m3u`
- EPG: `http://your-server-ip:8675/files/dtv_epg.xml`

**With PrismCast (live TV via Chromecast)**
1. Import `prismcast_channels.json` into PrismCast once (registers all channels)
2. Point your media server at:
   - M3U: `http://your-server-ip:8675/files/prismcast_enriched.m3u`
   - EPG: `http://your-server-ip:8675/files/dtv_epg.xml`

## Common Tasks

**Manual refresh** — click **Refresh Now** on the admin page.

**Re-authenticate** — click **Re-Authenticate** on the admin page if login fails. Auth tokens are automatically detected as stale and refreshed on each daily run.

**Update container**
```bash
docker compose pull && docker compose up -d --build
```

## Troubleshooting

**Container won't start** — check `DTV_USERNAME` and `DTV_PASSWORD` are set correctly. View logs in Portainer: Containers → directv-epg → Logs.

**Files not accessible from other devices** — set `HOST_IP` to your server's actual LAN IP, not `localhost`.

**EPG not updating** — trigger a manual refresh from the admin page, or check `REFRESH_HOUR` matches your expected timezone.

## Notes

- Requires an active DirecTV Stream subscription
- DirecTV Stream only, not compatible with DirecTV Satellite
- Web admin has no authentication — LAN use only; use a reverse proxy for external access

