#!/usr/bin/env python3
"""
daily_refresh.py

Runs the canonical DirecTV Stream pipeline (HAR-free):

1) capture_auth_context.py
2) fetch_allchannels_map.py
3) build_playback_map.py
4) build_channels_exports.py

Designed for Windows / Task Scheduler usage.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def run_step(label: str, argv: list[str], cwd: Path) -> None:
    print(f"[{ts()}] === {label} ===")
    print(f"[{ts()}] CMD: {' '.join(argv)}")
    t0 = time.time()
    try:
        subprocess.run(argv, cwd=str(cwd), check=True)
    except subprocess.CalledProcessError as e:
        dt = time.time() - t0
        print(f"[{ts()}] ERROR: Step failed after {dt:.1f}s: {label}")
        raise SystemExit(e.returncode) from e
    dt = time.time() - t0
    print(f"[{ts()}] OK: {label} ({dt:.1f}s)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data", help="Data/artifacts directory (default: data)")
    ap.add_argument("--out-dir", default="out", help="Output directory for deliverables (default: out)")
    ap.add_argument("--browser", default="firefox", choices=["chromium", "firefox", "webkit"], help="Playwright browser (default: firefox)")
    args = ap.parse_args()

    repo_dir = Path(__file__).resolve().parent
    py = sys.executable
    
    # Load .env file if it exists
    env_file = repo_dir / ".env"
    if env_file.exists():
        print(f"[{ts()}] Loading environment from {env_file}")
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key] = value

    # Script paths (absolute) so we don't depend on current working directory.
    capture = repo_dir / "capture_auth_context.py"
    allch = repo_dir / "fetch_allchannels_map.py"
    schedule = repo_dir / "fetch_dtv_schedule.py"
    playback = repo_dir / "build_playback_map.py"
    xmltv = repo_dir / "build_dtv_xmltv.py"
    exports = repo_dir / "build_channels_exports.py"

    for p in (capture, allch, schedule, playback, xmltv, exports):
        if not p.exists():
            print(f"[{ts()}] ERROR: Missing required script: {p}")
            return 2

    data_dir = (repo_dir / args.data_dir).resolve()
    out_dir = (repo_dir / args.out_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Intermediate files go in /data
    auth_context = data_dir / "auth_context.json"
    allchannels_csv = data_dir / "allchannels_map.csv"
    schedule_json = data_dir / "dtv_schedule_raw.json"
    playback_csv = data_dir / "playback_map.csv"
    
    # Final deliverables go in /out
    epg_xml = out_dir / "dtv_epg.xml"
    out_json = out_dir / "dtv_channels.json"
    out_m3u = out_dir / "dtv_channels.m3u"

    print(f"[{ts()}] Repo: {repo_dir}")
    print(f"[{ts()}] Data: {data_dir}")
    print(f"[{ts()}] Out:  {out_dir}")
    print(f"[{ts()}] Python: {py}")

    # 1) Auth capture - outputs to /data
    cap_args = [py, str(capture), "--headless", "--out-dir", str(data_dir)]
    if args.browser:
        cap_args.extend(["--browser", args.browser])

    run_step("capture_auth_context", cap_args, cwd=repo_dir)

    if not auth_context.exists():
        print(f"[{ts()}] ERROR: auth_context.json not found at {auth_context}")
        return 3

    # 2) Fetch AllChannels map
    run_step(
        "fetch_allchannels_map",
        [py, str(allch), "--auth-context", str(auth_context), "--out-dir", str(data_dir)],
        cwd=repo_dir,
    )

    if not allchannels_csv.exists():
        print(f"[{ts()}] ERROR: allchannels_map.csv not found at {allchannels_csv}")
        return 4

    # 3) Fetch schedule/EPG data (3 days by default)
    # Use smaller batch size to reduce API fallback rate
    run_step(
        "fetch_dtv_schedule",
        [
            py,
            str(schedule),
            "--auth-context", str(auth_context),
            "--allchannels", str(allchannels_csv),
            "--out-dir", str(data_dir),
            "--days", "3",              # 3 days of EPG
            "--window-hours", "6",      # 6-hour time windows
            "--max-channels", "20",     # Smaller batches = fewer fallbacks
            "--timeout", "30",          # Longer timeout for reliability
            "--retries", "3",           # Retry on failure
        ],
        cwd=repo_dir,
    )

    if not schedule_json.exists():
        print(f"[{ts()}] ERROR: dtv_schedule_raw.json not found at {schedule_json}")
        return 5

    # 4) Build playback map (authorization + manifest URLs + deeplinks)
    run_step(
        "build_playback_map",
        [
            py,
            str(playback),
            "--allchannels",
            str(allchannels_csv),
            "--auth-context",
            str(auth_context),
            "--out",
            str(playback_csv),
        ],
        cwd=repo_dir,
    )

    if not playback_csv.exists():
        print(f"[{ts()}] ERROR: playback_map.csv not found at {playback_csv}")
        return 6

    # 5) Build enhanced XMLTV (EPG with program data)
    run_step(
        "build_dtv_xmltv",
        [
            py,
            str(xmltv),
            "--schedule-json", str(schedule_json),
            "--allchannels", str(allchannels_csv),
            "--out-xml", str(epg_xml),
            "--dedupe",
        ],
        cwd=repo_dir,
    )

    if not epg_xml.exists():
        print(f"[{ts()}] ERROR: dtv_epg.xml not found at {epg_xml}")
        return 7

    # 6) Build exports (M3U/JSON only - skip channel list XML, we have enhanced EPG)
    temp_xml = data_dir / "channels_temp.xml"  # Dummy path, won't use this
    run_step(
        "build_channels_exports",
        [py, str(exports), str(playback_csv), str(allchannels_csv), str(out_json), str(temp_xml), str(out_m3u)],
        cwd=repo_dir,
    )
    
    # Clean up temp XML if it was created
    if temp_xml.exists():
        temp_xml.unlink()

    # Final summary
    print(f"[{ts()}] DONE")
    print(f"[{ts()}] Final deliverables:")
    print(f"[{ts()}]  - {epg_xml} (Enhanced XMLTV with EPG data)")
    print(f"[{ts()}]  - {out_m3u} (Channel playlist)")
    print(f"[{ts()}]  - {out_json} (Channel list JSON)")
    print(f"[{ts()}] Intermediate files in: {data_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
