#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

DEFAULT_BASE = "https://api.cld.dtvce.com"
SCHEDULE_PATH = "/discovery/edge/schedule/v1/service/schedule"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def to_ms(d: dt.datetime) -> int:
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


def chunked(items: List[str], n: int) -> Iterable[List[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _read_csv(path: str) -> List[Dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _truthy(val: Any) -> bool:
    s = "" if val is None else str(val).strip().lower()
    return s in ("1", "true", "yes", "y", "t", "on")


def _log(msg: str) -> None:
    print(msg, flush=True)


@dataclass
class AuthContext:
    base_url: str
    bearer: str
    client_context: str
    cookies: requests.cookies.RequestsCookieJar
    fis_properties: Optional[str] = None

    @staticmethod
    def from_json(
        path: str,
        base_url: str,
        bearer_override: str = "",
        client_context_override: str = "",
        fis_override: str = "",
    ) -> "AuthContext":
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        def find_first(obj: Any, keys: List[str]) -> Optional[Any]:
            if isinstance(obj, dict):
                lower_map = {str(k).lower(): k for k in obj.keys()}
                for k in keys:
                    kk = str(k).lower()
                    if kk in lower_map:
                        v = obj[lower_map[kk]]
                        if v is not None and str(v).strip() != "":
                            return v
                for v in obj.values():
                    found = find_first(v, keys)
                    if found is not None and str(found).strip() != "":
                        return found
            elif isinstance(obj, list):
                for it in obj:
                    found = find_first(it, keys)
                    if found is not None and str(found).strip() != "":
                        return found
            return None

        def find_auth_header(obj: Any) -> str:
            v = find_first(obj, ["authorization", "auth", "authHeader", "auth_header"])
            if isinstance(v, str) and v.lower().startswith("bearer "):
                return v
            headers = find_first(obj, ["headers", "requestHeaders", "request_headers"])
            if isinstance(headers, dict):
                for hk in ("authorization", "Authorization"):
                    hv = headers.get(hk)
                    if isinstance(hv, str) and hv.lower().startswith("bearer "):
                        return hv
            return ""

        bearer = (bearer_override or "").strip()
        if not bearer:
            token = find_first(
                raw,
                ["bearer", "bearer_token", "bearerToken", "access_token", "accessToken", "token"],
            )
            if isinstance(token, str) and token.strip():
                bearer = token.strip()
            else:
                ah = find_auth_header(raw)
                if ah:
                    bearer = ah.split(" ", 1)[1].strip()

        client_context = (client_context_override or "").strip()
        if not client_context:
            cc = find_first(raw, ["clientContext", "client_context", "clientContextStr", "client_context_str"])
            if isinstance(cc, str) and cc.strip():
                client_context = cc.strip()

        fis_properties = (fis_override or "").strip()
        if not fis_properties:
            fp = find_first(raw, ["fisProperties", "fis_properties"])
            if isinstance(fp, str) and fp.strip():
                fis_properties = fp.strip()

        # If your auth_context capture didn't include fisProperties, the schedule API may omit programme images.
        # The DirecTV web guide uses fisProperties to request specific artwork variants (HAR-backed).
        if not fis_properties:
            fis_properties = "bg-fplayer=2048*1152|iconic=250*144"

        if not bearer or not client_context:
            top_keys = list(raw.keys()) if isinstance(raw, dict) else []
            raise ValueError(
                "auth_context.json missing required fields. Need bearer token and clientContext.\n"
                f"Top-level keys seen: {top_keys}\n"
                "Fix options:\n"
                "  1) Re-run capture_auth_context.py\n"
                "  2) Or pass --bearer and --client-context explicitly\n"
            )

        jar = requests.cookies.RequestsCookieJar()
        cookies_raw = raw.get("cookies") if isinstance(raw, dict) else None
        if cookies_raw is None and isinstance(raw, dict):
            cookies_raw = raw.get("cookie_jar") or raw.get("cookieJar") or raw.get("cookiejar")

        if isinstance(cookies_raw, dict):
            for k, v in cookies_raw.items():
                if k and v is not None:
                    jar.set(k, str(v))
        elif isinstance(cookies_raw, list):
            for c in cookies_raw:
                if not isinstance(c, dict):
                    continue
                name = c.get("name")
                value = c.get("value")
                domain = c.get("domain")
                pathv = c.get("path")
                if name and value is not None:
                    kwargs = {}
                    if domain:
                        kwargs["domain"] = domain
                    if pathv:
                        kwargs["path"] = pathv
                    jar.set(name, str(value), **kwargs)

        return AuthContext(
            base_url=base_url,
            bearer=bearer,
            client_context=client_context,
            cookies=jar,
            fis_properties=fis_properties if fis_properties else None,
        )


def schedule_params(
    ctx: AuthContext,
    start_utc: dt.datetime,
    end_utc: dt.datetime,
    channel_ids: List[str],
    include_4k: bool,
    is_4k_compatible: bool,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "startTime": to_ms(start_utc),
        "endTime": to_ms(end_utc),
        "include4K": str(include_4k).lower(),
        "is4KCompatible": str(is_4k_compatible).lower(),
        "clientContext": ctx.client_context,
        "channelIds": channel_ids,
    }
    if ctx.fis_properties:
        params["fisProperties"] = ctx.fis_properties
    return params


def _session(ctx: AuthContext) -> requests.Session:
    s = requests.Session()
    s.cookies = ctx.cookies
    s.headers.update(
        {
            "Authorization": f"Bearer {ctx.bearer}",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Origin": "https://stream.directv.com",
            "Referer": "https://stream.directv.com/",
        }
    )
    return s


def _get_json(
    s: requests.Session,
    url: str,
    params: Dict[str, Any],
    timeout: int,
    retries: int,
    retry_backoff: float,
    debug: bool,
) -> Dict[str, Any]:
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            if debug:
                chans = params.get("channelIds") or []
                _log(
                    f"GET schedule startMs={params.get('startTime')} endMs={params.get('endTime')} "
                    f"channels={len(chans)} attempt={attempt+1}/{retries+1}"
                )
            t0 = time.time()
            r = s.get(url, params=params, timeout=timeout)
            elapsed = time.time() - t0
            if debug:
                _log(f"  -> HTTP {r.status_code} in {elapsed:.2f}s bytes={len(r.content)}")
            if r.status_code >= 400:
                body = (r.text or "")[:800]
                raise RuntimeError(f"HTTP {r.status_code} {r.reason}: {body}")
            return r.json()
        except Exception as e:
            last_err = e
            if attempt >= retries:
                break
            sleep_s = retry_backoff * (2**attempt)
            if debug:
                _log(f"  !! error: {e} (sleep {sleep_s:.1f}s)")
            time.sleep(sleep_s)
    raise RuntimeError(f"GET failed after retries: {last_err}") from last_err


def schedules_list(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    scheds = payload.get("schedules")
    return scheds if isinstance(scheds, list) else []


def validate_multi_channel(payload: Dict[str, Any], requested_channel_ids: List[str], min_ratio: float) -> bool:
    scheds = schedules_list(payload)
    req = set(requested_channel_ids)
    if not req:
        return True
    got_ids = {s.get("channelId") for s in scheds if isinstance(s, dict)}
    got_ids.discard(None)
    overlap = len(req.intersection(got_ids))
    if overlap == 0 and len(scheds) >= len(req):
        return True
    ratio = overlap / max(1, len(req))
    return ratio >= min_ratio


def combine_single_channel_payloads(channel_payloads: List[Tuple[str, Dict[str, Any]]]) -> Dict[str, Any]:
    combined: Dict[str, Any] = {"schedules": []}
    for channel_id, payload in channel_payloads:
        scheds = schedules_list(payload)
        if scheds:
            combined["schedules"].extend(scheds)
        else:
            combined["schedules"].append({"scheduleChannelId": channel_id, "contents": []})
    combined["_combined"] = True
    return combined


def build_channel_id_list(allchannels_csv: str, playback_csv: Optional[str], include_all: bool) -> List[str]:
    allch = _read_csv(allchannels_csv)
    rid_all = []
    for r in allch:
        rid = (r.get("resourceId") or r.get("resource_id") or "").strip()
        if rid:
            rid_all.append(rid)

    if include_all or not playback_csv:
        return sorted(set(rid_all))

    pb = _read_csv(playback_csv)
    playable_rids = set()
    for r in pb:
        if not _truthy(r.get("playable")):
            continue
        rid = (r.get("resourceId") or r.get("resource_id") or "").strip()
        if rid:
            playable_rids.add(rid)

    if not playable_rids:
        return sorted(set(rid_all))

    return sorted(set([rid for rid in rid_all if rid in playable_rids]))


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="fetch_dtv_schedule.py",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--auth-context", required=True, help="Path to out/auth_context.json")
    p.add_argument("--allchannels", required=True, help="Path to out/allchannels_map.csv")
    p.add_argument("--playback", default="", help="Path to out/playback_map.csv (for playable filter)")
    p.add_argument("--out-dir", required=True, help="Output directory")
    p.add_argument("--days", type=int, default=3, help="How many days ahead to fetch")
    p.add_argument("--window-hours", type=int, default=6, help="Window size in hours")
    p.add_argument("--max-channels", type=int, default=40, help="Max channels per multi-channel request")
    p.add_argument("--timeout", type=int, default=20, help="HTTP timeout seconds")
    p.add_argument("--retries", type=int, default=3, help="Retry count per request")
    p.add_argument("--retry-backoff", type=float, default=1.0, help="Backoff base seconds")
    p.add_argument("--min-ratio", type=float, default=0.75, help="Min overlap ratio for multi-channel response")
    p.add_argument("--include-all", action="store_true", help="Include non-playable channels too")
    p.add_argument("--include4k", action="store_true", help="Pass include4K=true")
    p.add_argument("--is4kcompatible", action="store_true", help="Pass is4KCompatible=true")
    p.add_argument("--base-url", default=DEFAULT_BASE, help="API base url")

    p.add_argument("--bearer", default="", help="Override bearer token (optional)")
    p.add_argument("--client-context", default="", help="Override clientContext (optional)")
    p.add_argument("--fis-properties", default="", help="Override fisProperties (optional)")

    p.add_argument("--debug", action="store_true", help="Enable verbose progress output")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ctx = AuthContext.from_json(
        args.auth_context,
        base_url=args.base_url,
        bearer_override=args.bearer,
        client_context_override=args.client_context,
        fis_override=args.fis_properties,
    )
    s = _session(ctx)

    playback_csv = args.playback.strip() or None
    channel_ids = build_channel_id_list(args.allchannels, playback_csv, include_all=args.include_all)

    if not channel_ids:
        _log("No channelIds found. Check allchannels_map.csv has resourceId column.")
        return 2

    start = utc_now()
    end = start + dt.timedelta(days=args.days)
    window = dt.timedelta(hours=args.window_hours)
    n_windows = int(math.ceil((end - start) / window))

    windows_path = out_dir / "schedule_windows.jsonl"
    combined_out = out_dir / "dtv_schedule_raw.json"
    url = ctx.base_url.rstrip("/") + SCHEDULE_PATH

    total_batches = math.ceil(len(channel_ids) / max(1, args.max_channels))
    total_requests_est = n_windows * total_batches

    _log(f"Schedule endpoint: {url}")
    _log(f"Channels: {len(channel_ids)} | windows: {n_windows} | est requests: {total_requests_est} | timeout: {args.timeout}s")

    meta = {
        "base_url": ctx.base_url,
        "schedule_path": SCHEDULE_PATH,
        "generated_utc": utc_now().isoformat(),
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
        "days": args.days,
        "window_hours": args.window_hours,
        "max_channels": args.max_channels,
        "include4K": bool(args.include4k),
        "is4KCompatible": bool(args.is4kcompatible),
        "channel_ids_count": len(channel_ids),
    }

    all_payloads: List[Dict[str, Any]] = []
    req_count = 0
    fallback_count = 0
    t_all = time.time()

    with open(windows_path, "w", encoding="utf-8", newline="\n") as wf:
        for wi in range(n_windows):
            w_start = start + wi * window
            w_end = min(end, w_start + window)
            if args.debug:
                _log(f"\n=== Window {wi+1}/{n_windows} {w_start.isoformat()} -> {w_end.isoformat()} ===")

            batch_i = 0
            for batch in chunked(channel_ids, args.max_channels):
                batch_i += 1
                req_count += 1
                if args.debug:
                    _log(f"-- Batch {batch_i}/{total_batches} (channels={len(batch)}) (req {req_count}/{total_requests_est})")

                params = schedule_params(
                    ctx,
                    w_start,
                    w_end,
                    batch,
                    include_4k=bool(args.include4k),
                    is_4k_compatible=bool(args.is4kcompatible),
                )

                payload = _get_json(
                    s,
                    url,
                    params=params,
                    timeout=args.timeout,
                    retries=args.retries,
                    retry_backoff=args.retry_backoff,
                    debug=args.debug,
                )

                if not validate_multi_channel(payload, batch, min_ratio=args.min_ratio):
                    fallback_count += 1
                    if args.debug:
                        _log("  !! Partial multi-channel response; falling back to per-channel for this batch")

                    chan_payloads: List[Tuple[str, Dict[str, Any]]] = []
                    for cid in batch:
                        req_count += 1
                        one_params = schedule_params(
                            ctx,
                            w_start,
                            w_end,
                            [cid],
                            include_4k=bool(args.include4k),
                            is_4k_compatible=bool(args.is4kcompatible),
                        )
                        one = _get_json(
                            s,
                            url,
                            params=one_params,
                            timeout=args.timeout,
                            retries=args.retries,
                            retry_backoff=args.retry_backoff,
                            debug=args.debug,
                        )
                        chan_payloads.append((cid, one))

                    payload = combine_single_channel_payloads(chan_payloads)
                    payload["_fallback_single_channel"] = True

                payload["_window"] = {
                    "start_utc": w_start.isoformat(),
                    "end_utc": w_end.isoformat(),
                    "start_ms": to_ms(w_start),
                    "end_ms": to_ms(w_end),
                    "batch_size": len(batch),
                }

                wf.write(json.dumps(payload, ensure_ascii=False) + "\n")
                all_payloads.append(payload)

                if not args.debug and (req_count % 2 == 0):
                    elapsed = time.time() - t_all
                    # Calculate actual progress
                    batches_done = (wi * total_batches) + batch_i
                    total_expected_batches = n_windows * total_batches
                    pct = (batches_done / total_expected_batches) * 100 if total_expected_batches > 0 else 0
                    _log(f"...progress: {req_count} requests | {batches_done}/{total_expected_batches} batches ({pct:.0f}%) | {elapsed:.1f}s | fallbacks={fallback_count}")

    combined = {"meta": meta, "payloads": all_payloads}
    with open(combined_out, "w", encoding="utf-8") as f:
        json.dump(combined, f, indent=2)

    elapsed = time.time() - t_all
    _log(f"\nWrote: {windows_path}")
    _log(f"Wrote: {combined_out}")
    _log(f"requests: {req_count} | fallbacks: {fallback_count} | elapsed: {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())