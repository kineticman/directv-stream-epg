#!/usr/bin/env python3
"""
build_prismcast_m3u.py

Generates two outputs from allchannels_map.csv:

  1. PrismCast channels JSON  (--out-json)
     Import once via PrismCast UI: Add/Import → Channels (JSON)
     Or via API: POST /config/channels/import

  2. Enriched M3U playlist    (--out-m3u)
     Ready-to-use M3U with:
       - PrismCast HLS stream URLs  (http://{host}:5589/hls/{key}/stream.m3u8)
       - tvg-id matching dtv_epg.xml  (dtv-{resourceId})
       - tvg-logo, tvg-chno, group-title
     Point your media server at this file + dtv_epg.xml for full EPG.

Usage:
  python build_prismcast_m3u.py \
    --allchannels data/allchannels_map.csv \
    --out-json    out/prismcast_channels.json \
    --out-m3u     out/prismcast_enriched.m3u \
    --prismcast-host 192.168.86.70 \
    --prismcast-port 5589

Notes:
  - PrismCast must have channels imported (via --out-json) for HLS URLs to work.
  - Channel keys are generated using PrismCast's generateChannelKey() algorithm
    (lowercase, non-alphanumeric to hyphens, deduplicated with ccid suffix).
  - PRISMCAST_HOST / PRISMCAST_PORT env vars can substitute for CLI flags.
"""

import argparse
import csv
import json
import os
import re


DTV_GUIDE_URL = "https://stream.directv.com/guide"
PRISMCAST_PROFILE = "directvStream"
GROUP_TITLE = "DirecTV Stream"


def _read_csv(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _channel_key(name):
    """
    Replicates PrismCast's generateChannelKey() from utils/index.js.
    Lowercase, collapse non-alphanumeric runs to hyphens, strip leading/trailing hyphens.
    """
    if not name:
        return ""
    key = name.lower().strip()
    key = re.sub(r"[^a-z0-9]+", "-", key)
    return key.strip("-")


def _m3u_escape(val):
    return (val or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def build_channels(rows):
    """
    Returns list of channel dicts with all fields needed for both JSON and M3U output.
    Handles duplicate key resolution.
    """
    seen_keys = {}
    channels = []
    skipped = 0

    for r in rows:
        resource_id = (r.get("resourceId") or "").strip()
        if not resource_id:
            skipped += 1
            continue

        ccid     = (r.get("ccid") or "").strip()
        number   = (r.get("channelNumber") or "").strip()
        callsign = (r.get("callSign") or "").strip()
        name     = (r.get("channelName") or callsign or f"DTV {ccid}").strip()
        logo     = (r.get("logoUrl") or "").strip()

        # Fall back to imageserver logo if none in CSV
        if not logo and resource_id:
            logo = f"https://dfwfis.prod.dtvcdn.com/catalog/image/imageserver/v1/service/channel/{resource_id}/chlogo-clb-guide/60/45"

        key = _channel_key(name)
        if not key:
            skipped += 1
            continue

        # Resolve duplicate keys by appending ccid
        if key in seen_keys:
            key = f"{key}-{ccid}"
        seen_keys[key] = True

        channels.append({
            "key":         key,
            "name":        name,
            "number":      number,
            "callsign":    callsign,
            "ccid":        ccid,
            "resource_id": resource_id,
            "xmltv_id":    f"dtv-{resource_id}",
            "logo":        logo,
        })

    return channels, skipped


def write_json(channels, path):
    """Write PrismCast channels JSON for import."""
    out = {}
    for ch in channels:
        entry = {
            "name":            ch["name"],
            "url":             DTV_GUIDE_URL,
            "profile":         PRISMCAST_PROFILE,
            "channelSelector": ch["resource_id"],
        }
        if ch["number"]:
            try:
                entry["channelNumber"] = int(ch["number"])
            except ValueError:
                entry["channelNumber"] = ch["number"]
        out[ch["key"]] = entry

    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
        f.write("\n")


def write_m3u(channels, path, host, port):
    """Write enriched M3U with PrismCast HLS URLs and tvg-id for EPG matching."""
    base_url = f"http://{host}:{port}"

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("#EXTM3U\n")
        for ch in channels:
            hls_url  = f"{base_url}/hls/{ch['key']}/stream.m3u8"
            xmltv_id = ch["xmltv_id"]
            name     = ch["name"]
            logo     = ch["logo"]
            number   = ch["number"]

            attrs = [
                f'tvg-id="{_m3u_escape(xmltv_id)}"',
                f'tvg-name="{_m3u_escape(name)}"',
                f'channel-id="{_m3u_escape(ch["key"])}"',
                f'group-title="{GROUP_TITLE}"',
            ]
            if number:
                attrs.append(f'channel-number="{_m3u_escape(number)}"')
                attrs.append(f'tvg-chno="{_m3u_escape(number)}"')
            if logo:
                attrs.append(f'tvg-logo="{_m3u_escape(logo)}"')

            f.write(f'#EXTINF:-1 {" ".join(attrs)},{name}\n')
            f.write(f"{hls_url}\n\n")


def main():
    ap = argparse.ArgumentParser(
        description="Generate PrismCast JSON import file and enriched M3U playlist",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--allchannels",     required=True,  help="Path to data/allchannels_map.csv")
    ap.add_argument("--out-json",        default="",     help="Output PrismCast channels JSON path (skip if empty)")
    ap.add_argument("--out-m3u",         default="",     help="Output enriched M3U path (skip if empty)")
    ap.add_argument("--prismcast-host",  default="",     help="PrismCast host IP (also: PRISMCAST_HOST env var)")
    ap.add_argument("--prismcast-port",  default="",     help="PrismCast port (also: PRISMCAST_PORT env var)")
    args = ap.parse_args()

    # Resolve host/port from args or env
    host = args.prismcast_host or os.getenv("PRISMCAST_HOST", "localhost")
    port = args.prismcast_port or os.getenv("PRISMCAST_PORT", "5589")

    rows = _read_csv(args.allchannels)
    channels, skipped = build_channels(rows)

    if not channels:
        print("ERROR: No channels with resourceId found in allchannels_map.csv")
        return 1

    if args.out_json:
        write_json(channels, args.out_json)
        print(f"Wrote JSON:  {args.out_json}  ({len(channels)} channels)")
        print(f"  Import via PrismCast UI: Add/Import -> Channels (JSON)")
        print(f"  Or via API: curl -X POST http://{host}:{port}/config/channels/import "
              f"-H 'Content-Type: application/json' -d @{args.out_json}")

    if args.out_m3u:
        write_m3u(channels, args.out_m3u, host, port)
        print(f"Wrote M3U:   {args.out_m3u}  ({len(channels)} channels)")
        print(f"  M3U URL:  http://{host}:8675/files/prismcast_enriched.m3u")
        print(f"  EPG URL:  http://{host}:8675/files/dtv_epg.xml")

    if skipped:
        print(f"Skipped: {skipped} channels (no resourceId)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
