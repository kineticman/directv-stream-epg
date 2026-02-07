# DirecTV Stream EPG Server

Automatically generate XMLTV EPG and M3U playlists from your DirecTV Stream subscription.

Perfect for Plex, Jellyfin, Emby, Channels DVR, and other media servers!

---

## What This Does

This Docker container:

- Logs into your DirecTV Stream account (once)
- Fetches your channel lineup and EPG data
- Updates automatically every day at 3 AM
- Serves files via web browser (no command line needed)
- Provides easy copy-paste URLs for your media server

---

## Quick Start (Portainer)

### 1. Add Stack in Portainer

1. Open Portainer
2. Go to Stacks → Add Stack
3. Name it: directv-epg
4. Paste this docker-compose:

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
```

5. Click Deploy the stack

---

## Set Your Credentials

In Portainer's Environment Variables section:

| Name | Value |
|------|-------|
| DTV_USERNAME | Your DirecTV email |
| DTV_PASSWORD | Your DirecTV password |
| HOST_IP | Your server's LAN IP (e.g. 192.168.1.100) |

Important:
Set actual values in Portainer Environment Variables or in a .env file.
Do NOT paste credentials directly into the compose file.

---

## Access Web Admin

Open in your browser:
http://your-server-ip:8675/

You’ll see:

- System status
- Output files with copy-paste URLs
- Live refresh logs
- Manual refresh button

---

## Using the Files

### For Plex / Jellyfin / Emby

EPG URL:
http://192.168.1.100:8675/files/dtv_epg.xml

M3U URL:
http://192.168.1.100:8675/files/dtv_channels.m3u

### For Channels DVR

Settings → DVR → Sources  
Add Custom Channels  
Paste the M3U URL  

Settings → Guide Data → XMLTV  
Paste the EPG URL

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| DTV_USERNAME | required | Your DirecTV email |
| DTV_PASSWORD | required | Your DirecTV password |
| HOST_IP | required | Your server's LAN IP |
| WEB_PORT | 8675 | Web admin port |
| REFRESH_HOUR | 3 | Daily refresh hour (0-23) |
| REFRESH_MINUTE | 0 | Daily refresh minute (0-59) |
| TZ | America/New_York | Your timezone |

---

## Common Tasks

Manual Refresh:
Open admin → click Refresh Now

View Logs:
Logs auto-refresh on the admin page.

Change Schedule:
Update REFRESH_HOUR or REFRESH_MINUTE and restart the stack.

Update Container:
docker-compose pull
docker-compose up -d

---

## Troubleshooting

Container won't start:
Check credentials are correct.

Files not accessible:
Ensure HOST_IP is set to your server’s LAN IP (not localhost).

EPG not updating:
Verify refresh schedule or run manual refresh.

Authentication failed:
Container will re-authenticate automatically on next refresh.

---

## Generated Files

| File | Purpose |
|------|---------|
| dtv_epg.xml | XMLTV EPG with program data |
| dtv_channels.m3u | Channel playlist |
| dtv_channels.json | Channel list in JSON format |

---

## Security Notes

- Credentials are stored in environment variables
- Never commit your .env file
- Web admin has no authentication (LAN use only)
- Use reverse proxy with auth for internet exposure

---

## FAQ

Can I watch live TV through this?
No. This only generates EPG data and channel lists.

Do I need a DirecTV Stream subscription?
Yes.

Will this work with DirecTV Satellite?
No. Stream only.

How often does it refresh?
Daily at 3:00 AM by default.

---

License: Personal use only. Requires active DirecTV Stream subscription.
