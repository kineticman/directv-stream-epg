#!/usr/bin/env python3
"""
build_playback_map.py

Build playback_map.csv by calling:
  GET https://api.cld.dtvce.com/right/authorization/channel/v1

Preferred auth:
  --auth-context out/auth_context.json   (from capture_auth_context.py)

Legacy fallback:
  --har your.har

CSV columns:
  ccid, channelNumber, callSign, channelName,
  channel_guid, deeplink,
  callsign_channel_token, playable, playable_reason,
  streamURL, fallbackStreamUrl, keyframeUrl, auth_url

Deeplink format:
  dtvnow://deeplink.directvnow.com/play/channel/<CALLSIGN>/<CHANNEL_GUID>

IMPORTANT:
- CHANNEL_GUID is NOT ccid. It comes from AllChannels as "resourceId" (UUID).
- Your current allchannels_map.csv header is:
    ccid,channelNumber,callSign,channelName,logoUrl,source_url
  so channel_guid/deeplink will be blank until you add resourceId to that CSV.

Rules:
- playable=true iff streamURL is present.
- If request fails (non-200 / timeout / parse error), still write playable=false row.
- GET-only. No proxies.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests


# ----------------------------
# Logging
# ----------------------------

def log(msg: str) -> None:
    print(msg, flush=True)

def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)

def err(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)


# ----------------------------
# CSV IO
# ----------------------------

@dataclass
class ChannelRow:
    ccid: str
    channel_number: str
    call_sign: str
    channel_name: str
    channel_guid: str  # resourceId / GUID (UUID). May be blank if not present in CSV.


PLAYBACK_FIELDS = [
    "ccid",
    "channelNumber",
    "callSign",
    "channelName",
    "channel_guid",
    "deeplink",
    "callsign_channel_token",
    "playable",
    "playable_reason",
    "streamURL",
    "fallbackStreamUrl",
    "keyframeUrl",
    "auth_url",
]

FAIL_FIELDS = ["ccid", "callsign_channel_token", "auth_status", "error", "auth_url"]


def ensure_parent_dir(path: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def load_allchannels_map(path: str) -> Tuple[List[ChannelRow], bool]:
    """
    Returns (channels, has_guid_column)
    """
    with open(path, "r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        if not r.fieldnames:
            return [], False

        header = {h.strip() for h in r.fieldnames if h}
        # common GUID column names you might add later
        guid_cols = {"resourceId", "channelGuid", "channel_guid", "guid", "id"}
        has_guid = any(c in header for c in guid_cols)

        out: List[ChannelRow] = []

        def pick(row: Dict[str, Any], *keys: str) -> str:
            for k in keys:
                v = row.get(k)
                if v is not None and str(v).strip():
                    return str(v).strip()
            return ""

        for row in r:
            ccid = pick(row, "ccid", "ccId", "channelId", "channel_id").strip()
            if not ccid:
                continue

            channel_number = pick(row, "channelNumber", "channel_number", "number").strip()
            call_sign = pick(row, "callSign", "callsign", "call_sign").strip()
            channel_name = pick(row, "channelName", "name", "displayName", "title").strip()

            channel_guid = pick(row, "resourceId", "channelGuid", "channel_guid", "guid", "id").strip()

            out.append(ChannelRow(
                ccid=ccid,
                channel_number=channel_number,
                call_sign=call_sign,
                channel_name=channel_name,
                channel_guid=channel_guid,
            ))

    # de-dupe by ccid
    seen = set()
    dedup: List[ChannelRow] = []
    for ch in out:
        if ch.ccid in seen:
            continue
        seen.add(ch.ccid)
        dedup.append(ch)

    return dedup, has_guid


def load_playback_map_dedup(path: str) -> Dict[str, Dict[str, str]]:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {}

    best: Dict[str, Dict[str, str]] = {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            ccid = (row.get("ccid") or "").strip()
            if not ccid:
                continue

            normalized = {k: (row.get(k) or "").strip() for k in PLAYBACK_FIELDS}
            prev = best.get(ccid)

            if prev is None:
                best[ccid] = normalized
            else:
                prev_play = (prev.get("playable") or "").lower().strip()
                new_play = (normalized.get("playable") or "").lower().strip()
                prev_stream = (prev.get("streamURL") or "").strip()
                new_stream = (normalized.get("streamURL") or "").strip()

                prev_good = (prev_play == "true") or bool(prev_stream)
                new_good = (new_play == "true") or bool(new_stream)

                if new_good and not prev_good:
                    best[ccid] = normalized

    return best


def write_playback_map(path: str, rows_by_ccid: Dict[str, Dict[str, str]]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PLAYBACK_FIELDS)
        w.writeheader()
        for ccid in sorted(rows_by_ccid.keys(), key=lambda x: int(x) if x.isdigit() else x):
            row = rows_by_ccid[ccid]
            safe_row = {k: (row.get(k) or "") for k in PLAYBACK_FIELDS}
            w.writerow(safe_row)
    os.replace(tmp, path)


def backfill_playable_fields(rows_by_ccid: Dict[str, Dict[str, str]]) -> None:
    for _, row in rows_by_ccid.items():
        play = (row.get("playable") or "").strip().lower()
        if play in ("true", "false"):
            continue
        stream = (row.get("streamURL") or "").strip()
        if stream:
            row["playable"] = "true"
            row["playable_reason"] = "streamURL_present_existing_row"
        else:
            row["playable"] = "false"
            row["playable_reason"] = "no_stream_existing_row"


def done_ccids(rows_by_ccid: Dict[str, Dict[str, str]]) -> set[str]:
    done: set[str] = set()
    for ccid, row in rows_by_ccid.items():
        playable = (row.get("playable") or "").strip().lower()
        stream = (row.get("streamURL") or "").strip()
        if playable == "true" or stream:
            done.add(ccid)
    return done


def compute_deeplink(call_sign: str, channel_guid: str) -> str:
    if not call_sign or not channel_guid:
        return ""
    return f"dtvnow://deeplink.directvnow.com/play/channel/{call_sign}/{channel_guid}"


# ----------------------------
# Auth Context (preferred)
# ----------------------------

def load_auth_context(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        ctx = json.load(f)
    if not isinstance(ctx, dict):
        raise ValueError("auth_context.json must be a JSON object")
    return ctx


def bearer_from_context(ctx: Dict[str, Any]) -> str:
    auth = ctx.get("authorization")
    if not isinstance(auth, str) or not auth.strip():
        raise ValueError("auth_context.json missing 'authorization'")
    auth = auth.strip()
    if auth.lower().startswith("bearer "):
        tok = auth.split(" ", 1)[1].strip()
        if not tok:
            raise ValueError("auth_context.json has empty Bearer token")
        return tok
    return auth


def cookies_from_context(ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    c = ctx.get("cookies")
    if isinstance(c, list):
        return [x for x in c if isinstance(x, dict)]
    return []


def _param_to_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list) and v:
        return str(v[0])
    return str(v)


def auth_template_from_context(ctx: Dict[str, Any], ccid_param_override: str = "") -> Tuple[str, Dict[str, str], str]:
    tpl = ctx.get("request_template")
    if not isinstance(tpl, dict):
        raise ValueError("auth_context.json missing request_template")

    scheme = str(tpl.get("scheme") or "").strip()
    netloc = str(tpl.get("netloc") or "").strip()
    path = str(tpl.get("path") or "").strip()
    if not (scheme and netloc and path):
        raise ValueError("request_template missing scheme/netloc/path")

    base_url = f"{scheme}://{netloc}{path}"

    raw_params = tpl.get("params")
    if not isinstance(raw_params, dict):
        raise ValueError("request_template.params must be an object")

    params: Dict[str, str] = {str(k): _param_to_str(v) for k, v in raw_params.items()}

    ccid_param = (ccid_param_override or str(tpl.get("ccid_param") or "")).strip()
    if not ccid_param:
        for candidate in ("ccid", "channelId", "channelID", "channel_id", "id"):
            if candidate in params:
                ccid_param = candidate
                break
    if not ccid_param:
        raise ValueError("Could not determine ccid param name; provide --ccid-param")

    return base_url, params, ccid_param


# ----------------------------
# HAR (legacy fallback)
# ----------------------------

def iter_har_entries(har_path: str) -> Iterable[Dict[str, Any]]:
    with open(har_path, "r", encoding="utf-8") as f:
        har = json.load(f)
    for ent in har.get("log", {}).get("entries", []) or []:
        if isinstance(ent, dict):
            yield ent


def har_req_headers(entry: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for h in entry.get("request", {}).get("headers", []) or []:
        n = (h.get("name") or "").strip().lower()
        v = (h.get("value") or "").strip()
        if n:
            out[n] = v
    return out


def extract_bearer_from_har(har_path: str) -> Optional[str]:
    for ent in iter_har_entries(har_path):
        hdrs = har_req_headers(ent)
        auth = (hdrs.get("authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            tok = auth.split(" ", 1)[1].strip()
            if tok:
                return tok
    return None


def extract_cookie_from_har(har_path: str) -> Optional[str]:
    for ent in iter_har_entries(har_path):
        hdrs = har_req_headers(ent)
        c = (hdrs.get("cookie") or "").strip()
        if c:
            return c
    return None


def extract_auth_template_from_har(har_path: str) -> Optional[Tuple[str, Dict[str, str]]]:
    best: Optional[Tuple[str, Dict[str, str]]] = None
    for ent in iter_har_entries(har_path):
        url = (ent.get("request", {}).get("url") or "").strip()
        if "/right/authorization/channel/v1" not in url:
            continue

        u = urlparse(url)
        base = f"{u.scheme}://{u.netloc}{u.path}"
        qs = parse_qs(u.query)

        params: Dict[str, str] = {}
        for k, vals in qs.items():
            if vals:
                params[k] = vals[0]

        if "clientcontext" in params and "clientContext" not in params:
            params["clientContext"] = params.pop("clientcontext")

        if "ccid" in params and "clientContext" in params:
            best = (base, params)

    return best


# ----------------------------
# HTTP
# ----------------------------

def build_session(
    bearer_token: str,
    cookie_header: Optional[str],
    user_agent: str,
    cookies_list: Optional[List[Dict[str, Any]]] = None,
) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {bearer_token}",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": user_agent,
        "Origin": "https://stream.directv.com",
        "Referer": "https://stream.directv.com/",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })

    if cookies_list:
        for c in cookies_list:
            try:
                name = c.get("name")
                value = c.get("value")
                domain = c.get("domain")
                path = c.get("path") or "/"
                if name and value and domain:
                    s.cookies.set(str(name), str(value), domain=str(domain), path=str(path))
            except Exception:
                continue

    if cookie_header:
        s.headers["Cookie"] = cookie_header

    return s


def is_retryable_status(code: int) -> bool:
    return code in (408, 425, 429, 500, 502, 503, 504)


def sleep_backoff(attempt: int, base: float, cap: float) -> None:
    exp = min(cap, base * (2 ** (attempt - 1)))
    jitter = random.uniform(0, exp * 0.25)
    time.sleep(exp + jitter)


def safe_get_json(
    sess: requests.Session,
    url: str,
    params: Dict[str, str],
    timeout: float,
    attempts: int,
    backoff_base: float,
    backoff_cap: float,
) -> Tuple[Optional[Dict[str, Any]], Optional[int], Optional[str], str]:
    prepared_url = requests.Request("GET", url, params=params).prepare().url
    last_err: Optional[str] = None
    last_status: Optional[int] = None

    for attempt in range(1, attempts + 1):
        try:
            resp = sess.get(url, params=params, timeout=timeout)
            last_status = resp.status_code

            if resp.status_code == 200:
                try:
                    return resp.json(), resp.status_code, None, prepared_url
                except Exception as je:
                    last_err = f"json_parse_error: {je}"
            else:
                snippet = (resp.text or "")[:300].replace("\n", " ").strip()
                last_err = f"http_{resp.status_code}: {snippet}"

            if last_status is not None and is_retryable_status(last_status) and attempt < attempts:
                sleep_backoff(attempt, backoff_base, backoff_cap)
                continue
            break

        except requests.RequestException as rexc:
            last_err = f"request_error: {rexc.__class__.__name__}: {rexc}"
            if attempt < attempts:
                sleep_backoff(attempt, backoff_base, backoff_cap)
                continue
            break

    return None, last_status, last_err, prepared_url


# ----------------------------
# Extraction + classification
# ----------------------------

def extract_stream_fallback_keyframe(j: Dict[str, Any]) -> Tuple[str, str, str]:
    pb = j.get("playbackData")
    if isinstance(pb, dict):
        stream = pb.get("streamURL") or pb.get("streamUrl") or pb.get("manifestUrl") or pb.get("manifestURL") or ""
        fallback = pb.get("fallbackStreamUrl") or pb.get("fallbackStreamURL") or pb.get("fallbackUrl") or ""
        keyframe = pb.get("keyframeUrl") or pb.get("keyframeURL") or ""
        return (
            stream if isinstance(stream, str) else "",
            fallback if isinstance(fallback, str) else "",
            keyframe if isinstance(keyframe, str) else "",
        )
    return "", "", ""


def summarize_no_stream_reason(j: Dict[str, Any]) -> str:
    for k in ("error", "errorCode", "errorMessage", "message", "detail", "reason"):
        v = j.get(k)
        if isinstance(v, str) and v.strip():
            return f"{k}={v.strip()[:180]}"
    authorized = j.get("authorized")
    all_events = j.get("allEventsAuthorized")
    has_pb = isinstance(j.get("playbackData"), dict)
    return f"authorized={authorized} allEventsAuthorized={all_events} has_playbackData={has_pb}"


def classify_playable(j: Dict[str, Any], stream_url: str) -> Tuple[bool, str]:
    if stream_url and stream_url.strip():
        return True, "streamURL_present"
    return False, "no_stream; " + summarize_no_stream_reason(j)


# ----------------------------
# Main
# ----------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--allchannels", required=True)
    ap.add_argument("--out", required=True)

    # Preferred
    ap.add_argument("--auth-context", default="", help="Path to auth_context.json (preferred)")
    ap.add_argument("--ccid-param", default="", help="Override ccid param name if needed")
    ap.add_argument("--bearer", default="", help="Optional override token (token only, no 'Bearer ')")

    # Legacy fallback
    ap.add_argument("--har", default="", help="Legacy HAR file (optional if --auth-context provided)")
    ap.add_argument("--cookie", default="", help="Legacy cookie header override (HAR mode)")

    ap.add_argument("--failures", default="")
    ap.add_argument("--user-agent", default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145 Safari/537.36")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--attempts", type=int, default=5)
    ap.add_argument("--backoff-base", type=float, default=1.0)
    ap.add_argument("--backoff-cap", type=float, default=20.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--flush-every", type=int, default=10)
    args = ap.parse_args()

    ensure_parent_dir(args.out)
    failures_csv = args.failures.strip() or os.path.join(os.path.dirname(os.path.abspath(args.out)), "playback_failures_v2.csv")
    ensure_parent_dir(failures_csv)

    channels, has_guid = load_allchannels_map(args.allchannels)
    if not channels:
        err(f"No channels loaded from: {args.allchannels}")
        return 2

    if not has_guid:
        warn(
            "allchannels_map.csv does not include channel GUID/resourceId; "
            "output columns channel_guid/deeplink will be blank until you add resourceId to that CSV."
        )

    rows_by_ccid = load_playback_map_dedup(args.out)
    backfill_playable_fields(rows_by_ccid)
    done = done_ccids(rows_by_ccid)

    todo = [c for c in channels if c.ccid not in done]
    if args.limit and args.limit > 0:
        todo = todo[: args.limit]

    bearer_token = args.bearer.strip()
    cookie_header: Optional[str] = None
    cookies_list: Optional[List[Dict[str, Any]]] = None

    auth_url: str
    template_params: Dict[str, str]
    ccid_param: str

    if args.auth_context.strip():
        if not os.path.exists(args.auth_context):
            err(f"auth_context not found: {args.auth_context}")
            return 2

        ctx = load_auth_context(args.auth_context)
        if not bearer_token:
            bearer_token = bearer_from_context(ctx)

        auth_url, template_params, ccid_param = auth_template_from_context(ctx, ccid_param_override=args.ccid_param.strip())
        cookies_list = cookies_from_context(ctx)

        log(f"Using auth_context.json template: {auth_url}")
        log(f"ccid param: {ccid_param}")

    else:
        if not args.har.strip():
            err("Provide either --auth-context or --har.")
            return 2
        if not os.path.exists(args.har):
            err(f"HAR not found: {args.har}")
            return 2

        if not bearer_token:
            bearer_token = (extract_bearer_from_har(args.har) or "").strip()

        cookie_header = args.cookie.strip() or (extract_cookie_from_har(args.har) or "")

        tmpl = extract_auth_template_from_har(args.har)
        if not tmpl:
            err("Could not find /right/authorization/channel/v1 request in HAR.")
            return 2

        auth_url, template_params = tmpl
        ccid_param = "ccid"
        log(f"Using HAR template: {auth_url}")

    if not bearer_token:
        err("Missing bearer token. Re-run capture_auth_context.py --login (preferred) or recapture HAR.")
        return 2

    sess = build_session(
        bearer_token=bearer_token,
        cookie_header=(cookie_header or None),
        user_agent=args.user_agent,
        cookies_list=cookies_list,
    )

    log(f"Loaded channels: {len(channels)}")
    log(f"Existing playback_map rows (deduped): {len(rows_by_ccid)}")
    log(f"DONE rows (playable): {len(done)}")
    log(f"To fetch: {len(todo)}")
    log(f"Output: {args.out}")
    log(f"Failures: {failures_csv}")

    fail_exists = os.path.exists(failures_csv) and os.path.getsize(failures_csv) > 0

    with open(failures_csv, "a", encoding="utf-8", newline="") as f_fail:
        fail_writer = csv.DictWriter(f_fail, fieldnames=FAIL_FIELDS)
        if not fail_exists:
            fail_writer.writeheader()

        for idx, ch in enumerate(todo, start=1):
            ccid = ch.ccid.strip()
            if not ccid:
                continue

            token = f"{ch.call_sign}-{ccid}.dfw.1080" if ch.call_sign else f"CCID-{ccid}"
            deeplink = compute_deeplink(ch.call_sign, ch.channel_guid)

            log(f"[{idx}/{len(todo)}] ccid={ccid} callSign={ch.call_sign or '-'} name={ch.channel_name or '-'}")

            params = dict(template_params)
            params[ccid_param] = ccid

            j, status, error, prepared_url = safe_get_json(
                sess=sess,
                url=auth_url,
                params=params,
                timeout=args.timeout,
                attempts=args.attempts,
                backoff_base=args.backoff_base,
                backoff_cap=args.backoff_cap,
            )

            if j is None:
                fail_writer.writerow({
                    "ccid": ccid,
                    "callsign_channel_token": token,
                    "auth_status": status if status is not None else "",
                    "error": error or "unknown_error",
                    "auth_url": prepared_url,
                })
                f_fail.flush()

                rows_by_ccid[ccid] = {
                    "ccid": ccid,
                    "channelNumber": ch.channel_number,
                    "callSign": ch.call_sign,
                    "channelName": ch.channel_name,
                    "channel_guid": ch.channel_guid,
                    "deeplink": deeplink,
                    "callsign_channel_token": token,
                    "playable": "false",
                    "playable_reason": f"request_failed; {error or 'unknown_error'}",
                    "streamURL": "",
                    "fallbackStreamUrl": "",
                    "keyframeUrl": "",
                    "auth_url": prepared_url,
                }

                if args.flush_every and (idx % args.flush_every == 0 or idx == len(todo)):
                    write_playback_map(args.out, rows_by_ccid)

                continue

            stream, fallback, keyframe = extract_stream_fallback_keyframe(j)
            playable_bool, playable_reason = classify_playable(j, stream)

            if not playable_bool:
                fail_writer.writerow({
                    "ccid": ccid,
                    "callsign_channel_token": token,
                    "auth_status": status if status is not None else "",
                    "error": playable_reason,
                    "auth_url": prepared_url,
                })
                f_fail.flush()

            rows_by_ccid[ccid] = {
                "ccid": ccid,
                "channelNumber": ch.channel_number,
                "callSign": ch.call_sign,
                "channelName": ch.channel_name,
                "channel_guid": ch.channel_guid,
                "deeplink": deeplink,
                "callsign_channel_token": token,
                "playable": "true" if playable_bool else "false",
                "playable_reason": playable_reason,
                "streamURL": stream.strip(),
                "fallbackStreamUrl": fallback.strip(),
                "keyframeUrl": keyframe.strip(),
                "auth_url": prepared_url,
            }

            if args.flush_every and (idx % args.flush_every == 0 or idx == len(todo)):
                write_playback_map(args.out, rows_by_ccid)

    write_playback_map(args.out, rows_by_ccid)
    log("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
