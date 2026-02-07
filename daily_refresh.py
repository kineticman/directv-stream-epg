#!/usr/bin/env python3
"""
daily_refresh.py â€” DirecTV Stream guide refresher + exporters

Goal (beta UX):
- Keep /out minimal: only the deliverables (XMLTV + M3U by default)
- Keep scrape/artifacts in /data (schedule JSON, channel map CSV, debug exports)

This script is a thin orchestrator around:
  - fetch_allchannels_map.py
  - fetch_dtv_schedule.py
  - build_dtv_xmltv.py
  - build_channels_exports.py   (optional: json/xml channel exports)

It supports both the older "schedule-*" flags and friendlier aliases:
  --days == --schedule-days
  --window-hours == --schedule-window-hours
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import subprocess
from pathlib import Path
from typing import List


def _now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{_now_stamp()}] {msg}", flush=True)


def _run(cmd: List[str], *, cwd: str | None = None) -> None:
    # show a readable one-liner (PowerShell-safe enough)
    log("CMD: " + " ".join(cmd))
    p = subprocess.run(cmd, cwd=cwd)
    if p.returncode != 0:
        raise SystemExit(p.returncode)


def _as_int(v: str | None, default: int | None = None) -> int | None:
    if v is None:
        return default
    try:
        return int(v)
    except Exception:
        return default


def parse_args(argv: List[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser()

    ap.add_argument("--repo", default=".", help="Folder containing the scripts (default: current directory)")
    ap.add_argument("--out-dir", default="out", help="Output folder for end-user deliverables (default: out)")
    ap.add_argument("--data-dir", default="data", help="Data/artifacts folder (default: data)")
    ap.add_argument("--python", default=sys.executable, help="Python interpreter to use for sub-scripts")

    # Login / browser (if fetch_dtv_schedule.py supports it)
    ap.add_argument("--auto-login", action="store_true", default=True, help="Attempt automated login if needed (default: True)")
    ap.add_argument("--headless", action="store_true", default=True, help="Run browser headless if login is needed (default: True)")
    ap.add_argument("--browser", choices=["chromium", "firefox", "webkit"], default="chromium", help="Browser for login (default: chromium)")

    # Friendly aliases
    ap.add_argument("--days", type=int, default=None, help="ALIAS for --schedule-days")
    ap.add_argument("--window-hours", type=int, default=None, help="ALIAS for --schedule-window-hours")

    # Existing schedule flags (kept for backward compatibility)
    ap.add_argument("--schedule-days", type=int, default=1, help="How many days to fetch schedule for (default: 1)")
    ap.add_argument("--schedule-window-hours", type=int, default=6, help="Guide window hours per request (default: 6)")
    ap.add_argument("--schedule-max-channels", type=int, default=40, help="Max channels per request batch (default: 40)")
    ap.add_argument("--schedule-timeout", type=int, default=30, help="HTTP timeout seconds (default: 30)")
    ap.add_argument("--schedule-retries", type=int, default=2, help="Retries per request (default: 2)")
    ap.add_argument("--schedule-retry-backoff", type=float, default=1.5, help="Retry backoff multiplier (default: 1.5)")
    ap.add_argument("--schedule-include-all", action="store_true", help="Include non-live channels too (passed through if supported)")

    # Outputs
    ap.add_argument("--m3u-url-mode", choices=["deeplink", "manifest", "fallback", "best"], default="best",
                    help="Which URL to put in M3U (default: best)")
    ap.add_argument("--no-dedupe", action="store_true", help="Disable programme dedupe in XMLTV")
    ap.add_argument("--emit-channel-exports", action="store_true",
                    help="Also emit dtv_channels.json + dtv_channels.xml (go to /data unless --emit-to-out)")
    ap.add_argument("--emit-to-out", action="store_true",
                    help="If set with --emit-channel-exports, write channel exports into /out (default: /data)")

    return ap.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)

    # Apply aliases if present
    if args.days is not None:
        args.schedule_days = args.days
    if args.window_hours is not None:
        args.schedule_window_hours = args.window_hours

    repo = Path(args.repo).resolve()
    out_dir = (repo / args.out_dir).resolve()
    data_dir = (repo / args.data_dir).resolve()
    auth_context = data_dir / "auth_context.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    py = args.python

    # Canonical paths
    allchannels_csv = data_dir / "allchannels_map.csv"
    schedule_json = data_dir / "dtv_schedule_raw.json"

    # Deliverables
    epg_xml = out_dir / "dtv_epg.xml"
    m3u_out = out_dir / "dtv_channels.m3u"

    # Optional extra exports
    channels_json = data_dir / "dtv_channels.json"
    channels_xml = data_dir / "dtv_channels.xml"

    t0 = time.time()

    # Step 0: Check for auth context
    if not auth_context.exists():
        log("=" * 80)
        log("AUTH CONTEXT NOT FOUND")
        log("")
        
        if args.auto_login:
            # Check if credentials are available
            username = os.getenv("DTV_EMAIL", "") or os.getenv("DTV_USERNAME", "")
            password = os.getenv("DTV_PASSWORD", "")
            
            if not username or not password:
                log("ERROR: --auto-login requires credentials via environment variables:")
                log("  DTV_EMAIL (or DTV_USERNAME)")
                log("  DTV_PASSWORD")
                log("")
                log("Docker: Add to docker-compose.yml or docker run:")
                log("  environment:")
                log("    - DTV_EMAIL=your.email@example.com")
                log("    - DTV_PASSWORD=yourpassword")
                log("")
                log("OR: Create auth_context.json manually:")
                log("  1. Run locally: python capture_auth_context.py --out-path ./data/auth_context.json")
                log("  2. Copy data/auth_context.json to container's /app/data/ volume")
                log("=" * 80)
                return 1
            
            log("Credentials found in environment, attempting auto-login...")
            log("=== capture_auth_context (auto-login) ===")
            cmd = [py, str(repo / "capture_auth_context.py"),
                   "--out-path", str(auth_context),
                   "--auto-login"]
            if args.headless:
                cmd.append("--headless")
            if args.browser:
                cmd += ["--browser", args.browser]
            try:
                _run(cmd)
                log(f"OK: capture_auth_context ({time.time() - t0:.1f}s)")
            except SystemExit as e:
                log(f"FAILED: capture_auth_context (exit code {e.code})")
                log("Check container logs for browser/login errors")
                return 1
            t0 = time.time()
        else:
            log("SETUP REQUIRED:")
            log("")
            log("Option 1: Create auth_context.json manually (Recommended)")
            log("  1. Run locally: python capture_auth_context.py --out-path ./data/auth_context.json")
            log("  2. Copy data/auth_context.json to container's /app/data/ volume")
            log("")
            log("Option 2: Use --auto-login with credentials")
            log("  Add to docker-compose.yml:")
            log("    environment:")
            log("      - DTV_EMAIL=your.email@example.com")
            log("      - DTV_PASSWORD=yourpassword")
            log("  Then: docker-compose up (will auto-capture on first run)")
            log("")
            log("=" * 80)
            return 1
    else:
        log(f"Using existing auth_context: {auth_context}")

    log("=== fetch_allchannels_map ===")
    _run([py, str(repo / "fetch_allchannels_map.py"),
          "--auth-context", str(auth_context),
          "--out-dir", str(data_dir),
          "--out-csv", str(allchannels_csv)])

    log(f"OK: fetch_allchannels_map ({time.time() - t0:.1f}s)")

    t1 = time.time()
    log("=== fetch_dtv_schedule ===")
    cmd = [py, str(repo / "fetch_dtv_schedule.py"),
           "--out-json", str(schedule_json),
           "--days", str(args.schedule_days),
           "--window-hours", str(args.schedule_window_hours),
           "--max-channels", str(args.schedule_max_channels),
           "--timeout", str(args.schedule_timeout),
           "--retries", str(args.schedule_retries),
           "--retry-backoff", str(args.schedule_retry_backoff)]
    if args.schedule_include_all:
        cmd.append("--include-all")
    if args.auto_login:
        cmd.append("--auto-login")
    if args.headless:
        cmd.append("--headless")
    if args.browser:
        cmd += ["--browser", args.browser]

    _run(cmd)
    log(f"OK: fetch_dtv_schedule ({time.time() - t1:.1f}s)")

    t2 = time.time()
    log("=== build_dtv_xmltv ===")
    cmd = [py, str(repo / "build_dtv_xmltv.py"),
           "--schedule-json", str(schedule_json),
           "--allchannels", str(allchannels_csv),
           "--out-xml", str(epg_xml)]
    if not args.no_dedupe:
        cmd.append("--dedupe")
    _run(cmd)
    log(f"OK: build_dtv_xmltv ({time.time() - t2:.1f}s)")

    t3 = time.time()
    log("=== build_channels_exports (m3u) ===")
    cmd = [py, str(repo / "build_channels_exports.py"),
           "--allchannels", str(allchannels_csv),
           "--out-m3u", str(m3u_out),
           "--url-mode", args.m3u_url_mode]
    if not args.emit_channel_exports:
        cmd += ["--only-m3u"]
    # default now: do NOT emit json/xml unless asked
    if args.emit_channel_exports:
        cmd += ["--out-json", str(channels_json), "--out-xml", str(channels_xml)]
    _run(cmd)
    log(f"OK: build_channels_exports ({time.time() - t3:.1f}s)")

    log("DONE")
    log(f"  - {epg_xml}")
    log(f"  - {m3u_out}")
    log(f"  - {schedule_json}")
    log(f"  - {allchannels_csv}")
    if args.emit_channel_exports:
        log(f"  - {channels_json}")
        log(f"  - {channels_xml}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))