#!/usr/bin/env python3
"""
build_dtv_xmltv.py

Converts DirecTV Stream schedule JSON (from fetch_dtv_schedule.py) into XMLTV.

Key mapping:
- DirecTV channel resourceId == schedule.schedules[*].channelId
- XMLTV channel id: "dtv-<resourceId>"  (matches build_channels_exports.py)

Inputs:
  --schedule-json out/dtv_schedule_raw.json
  --allchannels   out/allchannels_map.csv

Output:
  --out-xml       out/dtv_epg.xml
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from xml.sax.saxutils import escape

def _imageserver_chlogo(resource_id: str, *, w: int = 120, h: int = 90) -> str:
    """Stable channel logo URL used by the DirecTV Stream web guide."""
    rid = (resource_id or "").strip()
    if not rid:
        return ""
    return f"https://dfwfis.prod.dtvcdn.com/catalog/image/imageserver/v1/service/channel/{rid}/chlogo-clb-guide/{w}/{h}"



def _read_csv(path: str) -> List[Dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _parse_iso_z(s: str) -> Optional[dt.datetime]:
    if not s or not isinstance(s, str):
        return None
    # Examples: 2026-02-06T16:55:00Z
    try:
        if s.endswith("Z"):
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _xmltv_dt(ts: dt.datetime) -> str:
    # XMLTV format: YYYYMMDDHHMMSS +0000
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    ts_utc = ts.astimezone(dt.timezone.utc)
    return ts_utc.strftime("%Y%m%d%H%M%S") + " +0000"


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (int, float, bool)):
        return str(v)
    return str(v)


def _pick_first(*vals: Any) -> str:
    for v in vals:
        s = _text(v).strip()
        if s:
            return s
    return ""


def _as_list(v: Any) -> List[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


# ---------------------------------------------------------------------------
# Programme artwork (from schedule payload `images[]` when fisProperties is set)
# ---------------------------------------------------------------------------

_FIS_BASE = "https://dfwfis.prod.dtvcdn.com/catalog/image/imageserver/v1/service/sports"

def _fis_image_url(image_id: str, image_type: str, width: int, height: int) -> str:
    # DirecTV FIS /imageserver URL format observed in the web guide.
    return f"{_FIS_BASE}/{image_id}/{image_type}/{width}/{height}"

def _pick_program_icon(content: dict) -> str | None:
    imgs = content.get("images") or []
    if not isinstance(imgs, list) or not imgs:
        return None

    # Prefer 'iconic' (poster-ish) if present; fallback to bg-fplayer.
    def _score(im: dict) -> int:
        t = (im or {}).get("imageType")
        if t == "iconic":
            return 0
        if t == "bg-fplayer":
            return 1
        return 9

    imgs_sorted = sorted([im for im in imgs if isinstance(im, dict)], key=_score)
    for im in imgs_sorted:
        image_id = im.get("imageId")
        image_type = im.get("imageType")
        if not image_id or not image_type:
            continue
        if image_type == "iconic":
            return _fis_image_url(image_id, image_type, 250, 144)
        if image_type == "bg-fplayer":
            # Big hero art; scale down to something reasonable for XMLTV consumers.
            return _fis_image_url(image_id, image_type, 640, 360)

    return None


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="build_dtv_xmltv.py",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--schedule-json", required=True, help="Path to dtv_schedule_raw.json")
    p.add_argument("--allchannels", required=True, help="Path to allchannels_map.csv")
    p.add_argument("--out-xml", required=True, help="Output XMLTV path")
    p.add_argument("--source-info-url", default="https://stream.directv.com/guide", help="XMLTV <tv source-info-url>")
    p.add_argument("--source-info-name", default="DirecTV Stream", help="XMLTV <tv source-info-name>")
    p.add_argument("--dedupe", action="store_true", help="Dedupe programmes by scheduleId (recommended)")
    return p.parse_args(argv)


def _load_schedule(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_xml_header(out) -> None:
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')


def _write_tv_open(out, source_info_name: str, source_info_url: str) -> None:
    out.write(
        f'<tv generator-info-name="directv-stream-deeplinks" '
        f'source-info-name="{escape(source_info_name)}" '
        f'source-info-url="{escape(source_info_url)}">\n'
    )


def _write_tv_close(out) -> None:
    out.write("</tv>\n")


def _xml_tag(out, tag: str, text: str, attrs: Optional[Dict[str, str]] = None, indent: str = "  ") -> None:
    a = ""
    if attrs:
        a = " " + " ".join(f'{k}="{escape(v)}"' for k, v in attrs.items() if v is not None)
    out.write(f"{indent}<{tag}{a}>{escape(text)}</{tag}>\n")


def _xml_empty(out, tag: str, attrs: Optional[Dict[str, str]] = None, indent: str = "  ") -> None:
    a = ""
    if attrs:
        a = " " + " ".join(f'{k}="{escape(v)}"' for k, v in attrs.items() if v is not None)
    out.write(f"{indent}<{tag}{a} />\n")


def build_channel_map(allchannels_rows: List[Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    """
    Returns map keyed by resourceId -> {channelNumber, callSign, channelName, logoUrl, ccid}
    """
    m: Dict[str, Dict[str, str]] = {}
    for r in allchannels_rows:
        rid = (r.get("resourceId") or r.get("resource_id") or "").strip()
        if not rid:
            continue
        logo_url = (r.get("logoUrl") or r.get("logo_url") or "").strip()
        if not logo_url:
            # Matches the web guide's <img src=".../service/channel/{resourceId}/chlogo-clb-guide/60/45">
            logo_url = _imageserver_chlogo(rid)
        m[rid] = {
            "ccid": (r.get("ccid") or "").strip(),
            "channelNumber": (r.get("channelNumber") or r.get("channel_number") or "").strip(),
            "callSign": (r.get("callSign") or r.get("call_sign") or "").strip(),
            "channelName": (r.get("channelName") or r.get("channel_name") or "").strip(),
            "logoUrl": logo_url,
            "resourceId": rid,
        }
    return m


def iter_programmes(schedule_payloads: List[Dict[str, Any]]) -> Iterable[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    """
    Yields tuples: (channelId, content_obj, consumable_obj)
    """
    for payload in schedule_payloads:
        for sched in _as_list(payload.get("schedules")):
            if not isinstance(sched, dict):
                continue
            channel_id = _text(sched.get("channelId")).strip()
            for content in _as_list(sched.get("contents")):
                if not isinstance(content, dict):
                    continue
                consumables = _as_list(content.get("consumables"))
                if not consumables:
                    continue
                for cons in consumables:
                    if not isinstance(cons, dict):
                        continue
                    yield channel_id, content, cons


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    schedule = _load_schedule(args.schedule_json)
    payloads = schedule.get("payloads")
    if not isinstance(payloads, list):
        raise SystemExit("schedule-json does not contain a top-level 'payloads' list")

    allchannels = _read_csv(args.allchannels)
    chan_map = build_channel_map(allchannels)

    out_path = Path(args.out_xml)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    seen: Set[str] = set()
    prog_count = 0

    with open(out_path, "w", encoding="utf-8", newline="\n") as out:
        _write_xml_header(out)
        _write_tv_open(out, args.source_info_name, args.source_info_url)

        # Channels
        for rid, meta in sorted(chan_map.items(), key=lambda kv: (kv[1].get("channelNumber") or "99999", kv[1].get("callSign") or "")):
            xml_id = f"dtv-{rid}"
            out.write(f'  <channel id="{escape(xml_id)}">\n')
            # Display name: prefer channelName, fallback callSign
            display_name = _pick_first(meta.get("channelName"), meta.get("callSign"), rid)
            _xml_tag(out, "display-name", display_name, indent="    ")
            if meta.get("channelNumber"):
                _xml_tag(out, "display-name", meta["channelNumber"], attrs={"type": "number"}, indent="    ")
            if meta.get("callSign"):
                _xml_tag(out, "display-name", meta["callSign"], attrs={"type": "callsign"}, indent="    ")
            if meta.get("logoUrl"):
                _xml_empty(out, "icon", attrs={"src": meta["logoUrl"]}, indent="    ")
            out.write("  </channel>\n")

        # Programmes
        for channel_id, content, cons in iter_programmes(payloads):
            if not channel_id:
                continue

            # Dedup using scheduleId (best stable key we have from this endpoint)
            sched_id = _pick_first(cons.get("scheduleId"), cons.get("resourceId"), content.get("apgId"), content.get("canonicalId"))
            if args.dedupe and sched_id:
                if sched_id in seen:
                    continue
                seen.add(sched_id)

            start = _parse_iso_z(_text(cons.get("startTime")))
            stop = _parse_iso_z(_text(cons.get("endTime")))
            if not start or not stop:
                continue

            xml_channel = f"dtv-{channel_id}"
            out.write(
                f'  <programme start="{_xmltv_dt(start)}" stop="{_xmltv_dt(stop)}" channel="{escape(xml_channel)}">\n'
            )

            title = _pick_first(content.get("title"), content.get("displayTitle"), content.get("episodeTitle"))
            if title:
                _xml_tag(out, "title", title, indent="    ")
            sub = _pick_first(content.get("episodeTitle"), content.get("displayTitle") if content.get("displayTitle") != title else "")
            if sub:
                _xml_tag(out, "sub-title", sub, indent="    ")
            desc = _pick_first(content.get("description"))
            if desc:
                _xml_tag(out, "desc", desc, indent="    ")

            # Programme artwork (if present in the schedule payload)
            icon_url = _pick_program_icon(content)
            if icon_url:
                out.write(f'    <icon src="{escape(icon_url)}" />\n')

            # Categories: prefer genres; fall back to categories list
            genres = [g for g in _as_list(content.get("genres")) if isinstance(g, str)]
            cats = [c for c in _as_list(content.get("categories")) if isinstance(c, str)]
            for c in (genres or cats)[:6]:
                _xml_tag(out, "category", c, indent="    ")

            # Episode num: season/episode when available
            season = _text(content.get("seasonNumber")).strip()
            ep = _text(content.get("episodeNumber")).strip()
            if season or ep:
                # xmltv_ns uses zero-based season/episode; we don't always know if these are 1-based, but typically are.
                try:
                    s0 = int(season) - 1 if season else 0
                    e0 = int(ep) - 1 if ep else 0
                    out.write(f'    <episode-num system="xmltv_ns">{s0}.{e0}.</episode-num>\n')
                except Exception:
                    pass

            # IDs for debugging / downstream matching
            if content.get("tmsId"):
                _xml_tag(out, "episode-num", _text(content.get("tmsId")), attrs={"system": "tms"}, indent="    ")
            if sched_id:
                _xml_tag(out, "id", sched_id, indent="    ")

            # Live/OnNow badge hint
            badges = [b for b in _as_list(cons.get("badges")) if isinstance(b, str)]
            if "OnNow" in badges:
                _xml_empty(out, "live", indent="    ")

            out.write("  </programme>\n")
            prog_count += 1

        _write_tv_close(out)

    print(f"Wrote: {out_path}")
    print(f"Channels: {len(chan_map)} | programmes: {prog_count} | dedupe: {bool(args.dedupe)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())