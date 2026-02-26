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
import json
import requests
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

# PrismCast state
last_merge_time = None
last_merge_status = None  # 'success', 'error', or None


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
    # Get output files - EPG group
    epg_files = []
    for f in ['dtv_epg.xml', 'dtv_channels.m3u']:
        info = get_file_info(OUT_DIR / f)
        if info:
            info['url'] = f'/files/{f}'
            epg_files.append(info)

    # Get output files - PrismCast group
    prismcast_files = []
    for f in ['prismcast_enriched.m3u', 'prismcast_channels.json']:
        info = get_file_info(OUT_DIR / f)
        if info:
            info['url'] = f'/files/{f}'
            prismcast_files.append(info)
    
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
    
    prismcast_host = os.getenv('PRISMCAST_HOST', '').strip()
    return render_template(
        'admin.html',
        epg_files=epg_files,
        prismcast_files=prismcast_files,
        refresh_running=refresh_running,
        last_refresh_time=last_refresh_time.isoformat() if last_refresh_time else None,
        last_refresh_status=last_refresh_status,
        scheduler_info=scheduler_info,
        server_url=server_url,
        prismcast_host=prismcast_host,
        last_merge_time=last_merge_time.isoformat() if last_merge_time else None,
        last_merge_status=last_merge_status,
    )


@app.route('/api/status')
def api_status():
    """JSON status endpoint"""
    files = []
    for f in ['dtv_epg.xml', 'dtv_channels.m3u', 'prismcast_enriched.m3u', 'prismcast_channels.json']:
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


def get_prismcast_base():
    """Get PrismCast base URL from environment."""
    host = os.getenv('PRISMCAST_HOST', '').strip()
    port = os.getenv('PRISMCAST_PORT', '5589').strip()
    if not host:
        return None
    return f"http://{host}:{port}"


@app.route('/api/prismcast/status')
def api_prismcast_status():
    """Check if PrismCast is reachable and return channel count."""
    base = get_prismcast_base()
    if not base:
        return jsonify({'reachable': False, 'reason': 'PRISMCAST_HOST not set'})
    try:
        resp = requests.get(f"{base}/channels", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return jsonify({
                'reachable': True,
                'channel_count': data.get('count', 0),
                'base_url': base,
                'last_merge': last_merge_time.isoformat() if last_merge_time else None,
                'last_merge_status': last_merge_status,
            })
        return jsonify({'reachable': False, 'reason': f"HTTP {resp.status_code}"})
    except Exception as e:
        return jsonify({'reachable': False, 'reason': str(e)})


@app.route('/api/prismcast/merge', methods=['POST'])
def api_prismcast_merge():
    """Merge DirecTV channels into PrismCast (export → merge → import)."""
    global last_merge_time, last_merge_status

    base = get_prismcast_base()
    if not base:
        return jsonify({'error': 'PRISMCAST_HOST not set'}), 400

    allchannels_csv = DATA_DIR / 'allchannels_map.csv'
    if not allchannels_csv.exists():
        return jsonify({'error': 'allchannels_map.csv not found — run a refresh first'}), 400

    try:
        # Step 1: fetch current PrismCast channels
        export_resp = requests.get(f"{base}/config/channels/export", timeout=10)
        if export_resp.status_code != 200:
            raise RuntimeError(f"Export failed: HTTP {export_resp.status_code}")
        existing = export_resp.json()
        # export returns { channels: {...}, providerSelections: {...} } or just {...}
        if 'channels' in existing and isinstance(existing['channels'], dict):
            existing_channels = existing['channels']
        else:
            existing_channels = existing

        # Step 2: build DirecTV channels via build_prismcast_m3u.py
        sys.path.insert(0, str(APP_DIR))
        from build_prismcast_m3u import build_channels, _read_csv, write_json
        rows = _read_csv(str(allchannels_csv))
        dtv_channels, skipped = build_channels(rows)
        dtv_dict = {}
        for ch in dtv_channels:
            entry = {
                'name': ch['name'],
                'url': 'https://stream.directv.com/guide',
                'profile': 'directvStream',
                'channelSelector': ch['resource_id'],
            }
            if ch['number']:
                try:
                    entry['channelNumber'] = int(ch['number'])
                except ValueError:
                    entry['channelNumber'] = ch['number']
            dtv_dict[ch['key']] = entry

        # Step 3: merge — existing channels base, DirecTV keys overwrite
        merged = {**existing_channels, **dtv_dict}

        # Step 4: push merged channels back
        import_resp = requests.post(
            f"{base}/config/channels/import",
            json=merged,
            headers={'Content-Type': 'application/json'},
            timeout=15
        )
        if import_resp.status_code not in (200, 201):
            raise RuntimeError(f"Import failed: HTTP {import_resp.status_code} — {import_resp.text[:200]}")

        # Also write the JSON file as a local backup
        prismcast_json = OUT_DIR / 'prismcast_channels.json'
        import json as _json
        with open(prismcast_json, 'w') as f:
            _json.dump(dtv_dict, f, indent=2)
            f.write('\n')

        last_merge_time = datetime.now()
        last_merge_status = 'success'

        existing_count = len(existing_channels)
        dtv_count = len(dtv_dict)
        merged_count = len(merged)
        new_count = merged_count - existing_count

        logger.info(f"PrismCast merge: {existing_count} existing + {dtv_count} DirecTV ({new_count} new) = {merged_count} total")
        return jsonify({
            'message': f"Merged {dtv_count} DirecTV channels into PrismCast ({new_count} new, {dtv_count - new_count} updated). Total: {merged_count} channels.",
            'existing': existing_count,
            'dtv': dtv_count,
            'total': merged_count,
            'last_merge': last_merge_time.isoformat(),
        })

    except Exception as e:
        last_merge_time = datetime.now()
        last_merge_status = 'error'
        logger.error(f"PrismCast merge failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/files/<path:filename>')
def serve_file(filename):
    """Serve output files"""
    return send_from_directory(OUT_DIR, filename)


@app.route('/health')
def health():
    """Health check endpoint"""
    return 'OK', 200


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
