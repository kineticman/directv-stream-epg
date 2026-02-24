#!/usr/bin/env python3
"""
webapp.py - DirecTV Stream EPG Web Server with Admin Interface

Serves EPG files via HTTP and provides web admin for monitoring/control.
Uses APScheduler for daily refresh at configured time.
"""

import os
import sys
import subprocess
import logging
import socket
from datetime import datetime, time as dt_time
from pathlib import Path
from flask import Flask, render_template, jsonify, send_from_directory, request
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('/var/log/directv/webapp.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def get_server_ip():
    """Get the server's actual IP address"""
    # First check if HOST_IP is set (recommended for Docker)
    host_ip = os.getenv('HOST_IP', '').strip()
    if host_ip and not host_ip.startswith('172.') and not host_ip.startswith('127.'):
        return host_ip
    
    try:
        # Try to get non-Docker, non-localhost IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        
        # If it's a Docker internal IP (172.x.x.x), we need the host IP
        if ip.startswith('172.'):
            # Try reading from gateway (host machine)
            # In Docker bridge mode, host is typically at gateway + 1
            import subprocess
            try:
                result = subprocess.run(
                    ['ip', 'route', 'show', 'default'],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                if result.returncode == 0:
                    # Parse: "default via 172.22.0.1 dev eth0"
                    parts = result.stdout.split()
                    if 'via' in parts:
                        gateway = parts[parts.index('via') + 1]
                        # Gateway is the host in bridge mode
                        return gateway.replace('.1', '.0')  # Crude but often works
            except Exception:
                pass
        
        return ip
    except Exception:
        return "localhost"


# Configuration from environment
PORT = int(os.getenv('WEB_PORT', '8675'))
REFRESH_HOUR = int(os.getenv('REFRESH_HOUR', '3'))
REFRESH_MINUTE = int(os.getenv('REFRESH_MINUTE', '0'))
TIMEZONE = os.getenv('TZ', 'America/New_York')

# Paths
APP_DIR = Path('/app')
OUT_DIR = APP_DIR / 'out'
DATA_DIR = APP_DIR / 'data'
LOG_DIR = Path('/var/log/directv')
LOG_FILE = LOG_DIR / 'refresh.log'

# Ensure log directory and file exist
LOG_DIR.mkdir(parents=True, exist_ok=True)
if not LOG_FILE.exists():
    LOG_FILE.touch()

# Flask app
app = Flask(__name__, template_folder='/app/templates')
app.config['SECRET_KEY'] = os.urandom(24)

# Global state
refresh_running = False
last_refresh_time = None
last_refresh_status = None


def run_refresh():
    """Execute daily_refresh.py and capture output in real-time"""
    global refresh_running, last_refresh_time, last_refresh_status
    
    if refresh_running:
        logger.warning("Refresh already running, skipping")
        return
    
    refresh_running = True
    last_refresh_time = datetime.now()
    
    try:
        logger.info("Starting EPG refresh...")
        
        # Write header to log
        with open(LOG_FILE, 'a') as f:
            f.write(f"\n{'='*80}\n")
            f.write(f"Refresh started: {last_refresh_time.isoformat()}\n")
            f.write(f"{'='*80}\n")
            f.flush()
        
        # Run process with real-time output streaming
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'  # Force unbuffered output
        
        process = subprocess.Popen(
            [sys.executable, '-u', str(APP_DIR / 'daily_refresh.py')],  # -u for unbuffered
            cwd=str(APP_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=0,  # Unbuffered
            universal_newlines=True,
            env=env
        )
        
        # Stream output to log file in real-time
        with open(LOG_FILE, 'a', buffering=1) as f:  # Line buffered file
            for line in iter(process.stdout.readline, ''):
                if not line:
                    break
                f.write(line)
                f.flush()  # Flush immediately so live logs see it
                print(line, end='', flush=True)  # Also print to console
        
        # Wait for completion (with timeout)
        try:
            returncode = process.wait(timeout=1800)  # 30 min timeout
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = -1
            last_refresh_status = 'timeout'
            logger.error("EPG refresh timed out after 30 minutes")
            with open(LOG_FILE, 'a') as f:
                f.write("\n!!! TIMEOUT - Process killed after 30 minutes !!!\n")
            return
        
        # Write footer
        with open(LOG_FILE, 'a') as f:
            f.write(f"\n{'='*80}\n")
            f.write(f"Refresh completed: {datetime.now().isoformat()}\n")
            f.write(f"Return code: {returncode}\n")
            f.write(f"{'='*80}\n\n")
        
        if returncode == 0:
            last_refresh_status = 'success'
            logger.info("EPG refresh completed successfully")
        else:
            last_refresh_status = 'failed'
            logger.error(f"EPG refresh failed with code {returncode}")
            
    except subprocess.TimeoutExpired:
        last_refresh_status = 'timeout'
        logger.error("EPG refresh timed out after 30 minutes")
    except Exception as e:
        last_refresh_status = 'error'
        logger.error(f"EPG refresh error: {e}")
    finally:
        refresh_running = False


def get_file_info(filepath):
    """Get file info dict"""
    if not filepath.exists():
        return None
    
    stat = filepath.stat()
    return {
        'name': filepath.name,
        'size': stat.st_size,
        'size_mb': round(stat.st_size / 1024 / 1024, 2),
        'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
        'modified_ago': get_time_ago(datetime.fromtimestamp(stat.st_mtime))
    }


def get_time_ago(dt):
    """Human readable time ago"""
    delta = datetime.now() - dt
    
    if delta.days > 0:
        return f"{delta.days} day{'s' if delta.days > 1 else ''} ago"
    
    hours = delta.seconds // 3600
    if hours > 0:
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    
    minutes = delta.seconds // 60
    if minutes > 0:
        return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
    
    return "just now"


# Routes
@app.route('/')
def index():
    """Admin dashboard"""
    # Get output files
    files = []
    for f in ['dtv_epg.xml', 'dtv_channels.m3u', 'dtv_channels.json']:
        info = get_file_info(OUT_DIR / f)
        if info:
            info['url'] = f'/files/{f}'
            files.append(info)
    
    # Get scheduler info
    scheduler_info = {
        'next_run': None,
        'timezone': TIMEZONE,
        'schedule': f"{REFRESH_HOUR:02d}:{REFRESH_MINUTE:02d}"
    }
    
    if scheduler.get_jobs():
        next_run = scheduler.get_jobs()[0].next_run_time
        if next_run:
            scheduler_info['next_run'] = next_run.isoformat()
            scheduler_info['next_run_ago'] = get_time_ago(next_run.replace(tzinfo=None))
    
    # Get server IP
    server_ip = get_server_ip()
    server_url = f"http://{server_ip}:{PORT}"
    
    return render_template(
        'admin.html',
        files=files,
        refresh_running=refresh_running,
        last_refresh_time=last_refresh_time.isoformat() if last_refresh_time else None,
        last_refresh_status=last_refresh_status,
        scheduler_info=scheduler_info,
        server_url=server_url
    )


@app.route('/api/status')
def api_status():
    """JSON status endpoint"""
    files = []
    for f in ['dtv_epg.xml', 'dtv_channels.m3u', 'dtv_channels.json']:
        info = get_file_info(OUT_DIR / f)
        if info:
            files.append(info)
    
    return jsonify({
        'status': 'running',
        'refresh_running': refresh_running,
        'last_refresh': last_refresh_time.isoformat() if last_refresh_time else None,
        'last_status': last_refresh_status,
        'files': files
    })


@app.route('/api/logs')
def api_logs():
    """Get latest log entries"""
    lines = int(request.args.get('lines', 100))
    
    try:
        if not LOG_FILE.exists() or LOG_FILE.stat().st_size == 0:
            return jsonify({'logs': ['[No log entries yet - waiting for first refresh]\n']})
        
        with open(LOG_FILE, 'r', encoding='utf-8', errors='ignore') as f:
            all_lines = f.readlines()
            return jsonify({'logs': all_lines[-lines:] if all_lines else ['[Log file empty]\n']})
    except Exception as e:
        logger.error(f"Error reading logs: {e}")
        return jsonify({'logs': [f'[Error reading logs: {str(e)}]\n']})


@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    """Trigger manual refresh"""
    if refresh_running:
        return jsonify({'error': 'Refresh already running'}), 409
    
    logger.info("Manual refresh triggered via web interface")
    
    # Run in background
    import threading
    thread = threading.Thread(target=run_refresh)
    thread.daemon = True
    thread.start()
    
    return jsonify({'message': 'Refresh started'})


@app.route('/files/<path:filename>')
def serve_file(filename):
    """Serve output files"""
    return send_from_directory(OUT_DIR, filename)


@app.route('/health')
def health():
    """Health check endpoint"""
    return 'OK', 200


# ─────────────────────────────────────────────────────────────────────────────
# PrismCast integration
# ─────────────────────────────────────────────────────────────────────────────

def _load_channel_index() -> dict:
    """Load ccid → channel metadata from prismcast_channels.json (or allchannels fallback)."""
    # Prefer pre-built PrismCast channel file
    pc_path = OUT_DIR / 'prismcast_channels.json'
    if pc_path.exists():
        try:
            import json as _json
            data = _json.loads(pc_path.read_text(encoding='utf-8'))
            return {str(ch['ccid']): ch for ch in data.get('channels', [])}
        except Exception as e:
            logger.warning(f"Could not load prismcast_channels.json: {e}")

    # Fallback: read allchannels_map.csv directly
    csv_path = DATA_DIR / 'allchannels_map.csv'
    if csv_path.exists():
        import csv as _csv
        index = {}
        try:
            with open(csv_path, 'r', encoding='utf-8-sig', newline='') as f:
                for row in _csv.DictReader(f):
                    ccid = (row.get('ccid') or '').strip()
                    if ccid:
                        index[ccid] = {
                            'ccid': ccid,
                            'resourceId': (row.get('resourceId') or '').strip(),
                            'callSign':   (row.get('callSign') or '').strip(),
                            'name':       (row.get('channelName') or row.get('callSign') or f'CH {ccid}').strip(),
                            'number':     (row.get('channelNumber') or '').strip(),
                            'logo':       (row.get('logoUrl') or '').strip(),
                        }
        except Exception as e:
            logger.warning(f"Could not load allchannels_map.csv: {e}")
        return index

    return {}


@app.route('/prismcast/<ccid>')
def prismcast_resolver(ccid):
    """
    PrismCast channel resolver.

    PrismCast opens this URL in Chrome. The page:
      1. Looks up channel metadata (callSign, name) from local channel index
      2. Stores the target in sessionStorage
      3. Redirects Chrome to https://stream.directv.com/guide
      4. The Tampermonkey userscript picks up sessionStorage and performs:
         a. Click the channel tile  (aria-label="view {NAME}")
         b. Click the play button   (bg-image: mt_play_stroke_sm_dark4x.webp)
         c. DRM playback starts inside the authenticated browser

    Requires the Tampermonkey userscript dtv_prismcast.user.js to be installed
    in the Chrome instance PrismCast uses.
    """
    ccid = ccid.strip()
    index = _load_channel_index()
    ch = index.get(ccid)

    if not ch:
        # Unknown channel — still try; Tampermonkey will do its best
        ch = {'ccid': ccid, 'callSign': '', 'name': f'Channel {ccid}', 'resourceId': '', 'number': '', 'logo': ''}
        logger.warning(f"PrismCast resolver: unknown ccid={ccid}, channel index has {len(index)} entries")
    else:
        logger.info(f"PrismCast resolver: ccid={ccid} → {ch.get('callSign')} / {ch.get('name')}")

    import json as _json

    # Serialize target for sessionStorage injection
    target_json = _json.dumps({
        'ccid':     ch.get('ccid', ccid),
        'callSign': ch.get('callSign', ''),
        'name':     ch.get('name', ''),
        'resourceId': ch.get('resourceId', ''),
    })

    channel_display = ch.get('callSign') or ch.get('name') or f'Channel {ccid}'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Tuning {channel_display}...</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      background: #000;
      color: #fff;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      display: flex;
      align-items: center;
      justify-content: center;
      height: 100vh;
      flex-direction: column;
      gap: 20px;
    }}
    .spinner {{
      width: 48px; height: 48px;
      border: 4px solid #333;
      border-top-color: #0099d6;
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    h2 {{ font-size: 1.4rem; font-weight: 500; color: #e0e0e0; }}
    p  {{ font-size: 0.85rem; color: #666; }}
  </style>
</head>
<body>
  <div class="spinner"></div>
  <h2>Tuning to {channel_display}</h2>
  <p>Launching DirecTV Stream&hellip;</p>

  <script>
    // Store channel target for Tampermonkey to pick up on stream.directv.com
    try {{
      sessionStorage.setItem('prismcast_target', {target_json!r});
    }} catch(e) {{
      // sessionStorage may not persist across origin redirect; use URL hash as fallback
      console.warn('[PrismCast resolver] sessionStorage write failed:', e);
    }}

    // Small delay so the page visually confirms, then redirect
    setTimeout(function() {{
      window.location.replace('https://stream.directv.com/guide');
    }}, 400);
  </script>
</body>
</html>"""

    from flask import Response
    return Response(html, mimetype='text/html')


@app.route('/prismcast')
def prismcast_index():
    """List all channels available for PrismCast with their resolver URLs."""
    server_ip  = get_server_ip()
    base_url   = f"http://{server_ip}:{PORT}"
    index      = _load_channel_index()
    channels   = sorted(index.values(), key=lambda c: (
        int(c.get('number') or 999999) if (c.get('number') or '').isdigit() else 999999,
        c.get('name') or ''
    ))

    rows_html = ''.join(
        f'<tr>'
        f'<td>{c.get("number") or "—"}</td>'
        f'<td>{c.get("callSign") or "—"}</td>'
        f'<td>{c.get("name") or "—"}</td>'
        f'<td><code>{base_url}/prismcast/{c["ccid"]}</code></td>'
        f'</tr>'
        for c in channels
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>PrismCast Channel Index</title>
  <style>
    body {{ font-family: monospace; background: #111; color: #ccc; padding: 20px; }}
    h1   {{ color: #0099d6; margin-bottom: 10px; }}
    p    {{ color: #888; margin-bottom: 20px; font-size: 0.9rem; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ padding: 6px 12px; text-align: left; border-bottom: 1px solid #333; }}
    th {{ color: #0099d6; }}
    code {{ color: #aef; font-size: 0.85rem; }}
    tr:hover {{ background: #1a1a1a; }}
  </style>
</head>
<body>
  <h1>PrismCast Channel Resolver</h1>
  <p>{len(channels)} channels &mdash; resolver base: <code>{base_url}/prismcast/&lt;ccid&gt;</code></p>
  <p>Install <a href="/dtv-inject.js" style="color:#0099d6">dtv_prismcast.user.js</a> in Tampermonkey,
     then use these URLs in PrismCast.</p>
  <table>
    <thead><tr><th>#</th><th>Call Sign</th><th>Name</th><th>Resolver URL</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</body>
</html>"""

    from flask import Response
    return Response(html, mimetype='text/html')


@app.route('/dtv-inject.js')
def serve_userscript():
    """Serve the Tampermonkey userscript for easy installation."""
    userscript_path = APP_DIR / 'dtv_prismcast.user.js'
    if userscript_path.exists():
        content = userscript_path.read_text(encoding='utf-8')
        from flask import Response
        return Response(content, mimetype='application/javascript')
    from flask import abort
    abort(404)


def main():
    """Main entry point"""
    logger.info(f"Starting DirecTV EPG Web Server on port {PORT}")
    logger.info(f"Scheduled refresh: {REFRESH_HOUR:02d}:{REFRESH_MINUTE:02d} {TIMEZONE}")
    
    # Setup scheduler
    global scheduler
    scheduler = BackgroundScheduler(timezone=pytz.timezone(TIMEZONE))
    
    # Add daily refresh job
    scheduler.add_job(
        run_refresh,
        trigger=CronTrigger(hour=REFRESH_HOUR, minute=REFRESH_MINUTE),
        id='daily_refresh',
        name='Daily EPG Refresh',
        misfire_grace_time=1800  # 30 minute grace period
    )
    
    scheduler.start()
    logger.info("Scheduler started")
    
    # Run initial refresh on startup (in background after 2 min delay)
    import threading
    def delayed_initial_refresh():
        import time
        time.sleep(120)  # Wait 2 minutes for system to stabilize
        logger.info("Running initial refresh...")
        run_refresh()
    
    thread = threading.Thread(target=delayed_initial_refresh)
    thread.daemon = True
    thread.start()
    
    # Start Flask
    app.run(host='0.0.0.0', port=PORT, debug=False)


if __name__ == '__main__':
    main()
