# 📺 DirecTV Stream EPG Server

**Automatically generate XMLTV EPG and M3U playlists from your DirecTV Stream subscription.**

Perfect for Plex, Jellyfin, Emby, Channels DVR, and other media servers!

## ✨ What This Does

This Docker container:
- 🔄 Logs into your DirecTV Stream account (once)
- 📡 Fetches your channel lineup and EPG data
- 📅 Updates automatically every day at 3 AM
- 🌐 Serves files via web browser (no command line needed!)
- 📋 Provides easy copy-paste URLs for your media server

## 🎯 Quick Start (Portainer)

### 1. Add Stack in Portainer

1. Open Portainer
2. Go to **Stacks** → **Add Stack**
3. Name it: `directv-epg`
4. Paste this docker-compose:

```yaml
version: '3.8'

services:
  directv-epg:
    image: ghcr.io/kineticman/directv-stream-epg:latest  # Or build locally
    container_name: directv-epg
    restart: unless-stopped
    
    ports:
      - "8675:8675"
    
    volumes:
      - ./data:/app/data
      - ./out:/app/out
      - ./logs:/var/log/directv
    
    environment:
      # REQUIRED: Your DirecTV credentials
      - DTV_USERNAME=your-email@example.com
      - DTV_PASSWORD=your-password
      
      # REQUIRED: Your server's IP address
      - HOST_IP=192.168.1.100
      
      # Optional: Customize these
      - WEB_PORT=8675
      - REFRESH_HOUR=3
      - REFRESH_MINUTE=0
      - TZ=America/New_York
```

5. Click **Deploy the stack**

### 2. Set Your Credentials

In the environment variables section:
- `DTV_USERNAME` → Your DirecTV email
- `DTV_PASSWORD` → Your DirecTV password  
- `HOST_IP` → Your server's IP (e.g., `192.168.1.100`)

### 3. Access Web Admin

Open in your browser: `http://your-server-ip:8675/`

You'll see:
- ✅ System status
- 📂 Output files with copy-paste URLs
- 📊 Live refresh logs
- 🔄 Manual refresh button

## 📋 Using the Files

### For Plex / Jellyfin / Emby

1. Go to your admin page: `http://your-server-ip:8675/`
2. Click **📋 Copy URL** next to each file
3. Paste into your media server:
   - **EPG URL**: `http://192.168.1.100:8675/files/dtv_epg.xml`
   - **M3U URL**: `http://192.168.1.100:8675/files/dtv_channels.m3u`

### For Channels DVR

1. Settings → DVR → Sources
2. Add Custom Channels
3. Paste the M3U URL
4. Settings → Guide Data → XMLTV
5. Paste the EPG URL

## ⚙️ Configuration

All settings are configured via environment variables in Portainer:

| Variable | Default | Description |
|----------|---------|-------------|
| `DTV_USERNAME` | *(required)* | Your DirecTV email |
| `DTV_PASSWORD` | *(required)* | Your DirecTV password |
| `HOST_IP` | *(required)* | Your server's LAN IP |
| `WEB_PORT` | `8675` | Web admin port |
| `REFRESH_HOUR` | `3` | Daily refresh hour (0-23) |
| `REFRESH_MINUTE` | `0` | Daily refresh minute (0-59) |
| `TZ` | `America/New_York` | Your timezone |

### Common Timezones
- `America/New_York` (Eastern)
- `America/Chicago` (Central)
- `America/Denver` (Mountain)
- `America/Los_Angeles` (Pacific)

## 🔧 Common Tasks

### Manual Refresh
1. Open admin: `http://your-server-ip:8675/`
2. Click **🔄 Refresh Now**
3. Watch live logs for progress

### View Logs
Logs auto-refresh on the admin page. Click **🔄 Refresh Logs** for latest.

### Change Schedule
Edit environment variable in Portainer:
- `REFRESH_HOUR=5` (5 AM instead of 3 AM)

Then restart the stack.

### Update Container
```bash
docker-compose pull
docker-compose up -d
```

Or in Portainer: Stack → Editor → Pull & Redeploy

## 🐛 Troubleshooting

### Container won't start
**Check credentials**: Make sure `DTV_USERNAME` and `DTV_PASSWORD` are correct.

View logs in Portainer: Containers → directv-epg → Logs

### Files not accessible from other devices
**Fix HOST_IP**: Set to your server's actual IP address (not `localhost` or `127.0.0.1`)

Example: `HOST_IP=192.168.1.100`

### EPG not updating
**Check schedule**: Refresh happens at `REFRESH_HOUR` in your timezone.

**Manual refresh**: Use the web admin button to trigger immediately.

### Authentication failed
Your session may have expired. The container will re-authenticate automatically on next refresh.

## 📊 What Files Are Generated

| File | Size | Purpose |
|------|------|---------|
| `dtv_epg.xml` | ~8 MB | XMLTV EPG with 3 days of program data |
| `dtv_channels.m3u` | ~200 KB | Channel playlist with URLs |
| `dtv_channels.json` | ~200 KB | Channel list in JSON format |

## 🔒 Security Notes

- Your credentials are stored in environment variables
- Never commit your `.env` file to git
- The web admin has no authentication (LAN use only)
- For internet access, use a reverse proxy with auth

## ❓ FAQ

**Q: Can I watch live TV through this?**  
A: No. This only generates EPG data and channel lists for integration with other apps.

**Q: Do I need a DirecTV Stream subscription?**  
A: Yes. You must have an active DirecTV Stream account.

**Q: Will this work with DirecTV Satellite?**  
A: No. This is specifically for DirecTV Stream (formerly AT&T TV Now).

**Q: How often does it refresh?**  
A: Daily at 3:00 AM by default (configurable).

**Q: Can I run multiple instances?**  
A: Yes, but use different ports for each instance.

**Q: Does it work with VPN?**  
A: Yes, as long as the container can reach DirecTV's servers.

## 🆘 Support

**Issues**: Check the live logs on the admin page first.

**Help**: Create an issue on GitHub with:
- Container logs
- Your environment variables (redact password!)
- What you tried

## 📜 License

Personal use only. Requires active DirecTV Stream subscription.

---

**Quick Links:**
- 🌐 Web Admin: `http://your-server:8675/`
- 📺 EPG: `http://your-server:8675/files/dtv_epg.xml`
- 📋 M3U: `http://your-server:8675/files/dtv_channels.m3u`
