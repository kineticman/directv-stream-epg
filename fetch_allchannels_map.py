#!/usr/bin/env python3
"""
fetch_allchannels_map.py

HAR-free fetch of DirecTV Stream AllChannels metadata.

Uses auth_context.json created by capture_auth_context.py
to reuse Authorization, cookies, and the captured clientContext template.

Endpoint:
  https://api.cld.dtvce.com/discovery/metadata/channel/v5/service/allchannels

Outputs:
  - out/allchannels_raw.json (default)
  - out/allchannels_map.csv with columns:
      ccid,channelNumber,callSign,channelName,logoUrl,resourceId

Notes:
- CHANNEL_GUID for deeplink generation is "resourceId" (UUID), not ccid.
- GET-only. No proxies. No HTML scraping.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


ALLCHANNELS_URL = "https://api.cld.dtvce.com/discovery/metadata/channel/v5/service/allchannels"

def _imageserver_chlogo(resource_id: str, *, w: int = 120, h: int = 90) -> str:
    """Stable channel logo URL used by the DirecTV Stream web guide."""
    rid = (resource_id or "").strip()
    if not rid:
        return ""
    return f"https://dfwfis.prod.dtvcdn.com/catalog/image/imageserver/v1/service/channel/{rid}/chlogo-clb-guide/{w}/{h}"



def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


def err(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)


def ensure_parent_dir(path: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


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


def get_client_context_from_template(ctx: Dict[str, Any]) -> str:
    tpl = ctx.get("request_template")
    if not isinstance(tpl, dict):
        raise ValueError("auth_context.json missing request_template")
    params = tpl.get("params")
    if not isinstance(params, dict):
        raise ValueError("auth_context.json request_template.params missing/invalid")
    # Seen in your captured URL as "clientContext"
    cc = params.get("clientContext") or params.get("clientcontext")
    if isinstance(cc, list) and cc:
        cc = cc[0]
    if not isinstance(cc, str) or not cc.strip():
        raise ValueError("auth_context.json request_template.params missing clientContext")
    return cc.strip()


def build_session(bearer_token: str, cookies_list: List[Dict[str, Any]], user_agent: str) -> requests.Session:
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

    # apply cookies from storageState capture (if present)
    for c in cookies_list or []:
        try:
            name = c.get("name")
            value = c.get("value")
            domain = c.get("domain")
            path = c.get("path") or "/"
            if name and value and domain:
                s.cookies.set(str(name), str(value), domain=str(domain), path=str(path))
        except Exception:
            continue

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
) -> Dict[str, Any]:
    last_err: Optional[str] = None
    last_status: Optional[int] = None

    for attempt in range(1, attempts + 1):
        try:
            resp = sess.get(url, params=params, timeout=timeout)
            last_status = resp.status_code
            if resp.status_code == 200:
                try:
                    j = resp.json()
                    return j  # type: ignore[return-value]
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

    raise RuntimeError(last_err or "unknown_error")


# ----------------------------
# JSON extraction (shape-flexible)
# ----------------------------

def _looks_like_channel_obj(d: Dict[str, Any]) -> bool:
    keys = {k.lower() for k in d.keys()}
    # common hints
    return (
        "ccid" in keys or "callsign" in keys or "callSign".lower() in keys or
        "channelnumber" in keys or "resourceid" in keys or "logo" in keys or "logourl" in keys
    )


def find_best_channel_list(root: Any, max_depth: int = 6) -> List[Dict[str, Any]]:
    """
    Walk the JSON and find the largest list of dicts that look like channels.
    """
    best: List[Dict[str, Any]] = []

    def walk(node: Any, depth: int) -> None:
        nonlocal best
        if depth > max_depth:
            return

        if isinstance(node, list):
            if node and all(isinstance(x, dict) for x in node):
                dicts = node  # type: ignore[assignment]
                score = sum(1 for x in dicts if _looks_like_channel_obj(x))
                # prefer lists where many items look like channels, and list is large
                if score > 0:
                    if (score, len(dicts)) > (sum(1 for x in best if _looks_like_channel_obj(x)), len(best)):
                        best = dicts
            for x in node:
                walk(x, depth + 1)
            return

        if isinstance(node, dict):
            for v in node.values():
                walk(v, depth + 1)

    walk(root, 0)
    return best


def pick_str(d: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        if k in d:
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            # sometimes ints
            if isinstance(v, (int, float)) and str(v).strip():
                return str(v).strip()
    return ""


def extract_logo_url(d: Dict[str, Any]) -> str:
    # direct
    u = pick_str(d, "logoUrl", "logoURL", "logo_url")
    if u:
        return u

    # nested patterns
    logo = d.get("logo")
    if isinstance(logo, dict):
        u = pick_str(logo, "url", "href", "uri")
        if u:
            return u

    images = d.get("images")
    if isinstance(images, list):
        for item in images:
            if isinstance(item, dict):
                u = pick_str(item, "url", "href", "uri", "logoUrl")
                if u:
                    return u

    return ""


def normalize_channel_row(d: Dict[str, Any]) -> Dict[str, str]:
    ccid = pick_str(d, "ccid", "ccId", "channelId", "channelID", "channel_id", "id")
    channel_number = pick_str(d, "channelNumber", "channel_number", "number", "chNum", "chNumber")
    call_sign = pick_str(d, "callSign", "callsign", "call_sign")
    channel_name = pick_str(d, "channelName", "name", "displayName", "title")
    resource_id = pick_str(d, "resourceId", "resourceID", "resource_id", "guid")

    logo_url = extract_logo_url(d)

    return {
        "ccid": ccid,
        "channelNumber": channel_number,
        "callSign": call_sign,
        "channelName": channel_name,
        "logoUrl": logo_url,
        "resourceId": resource_id,
    }


def dedup_by_ccid(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen: set[str] = set()
    for r in rows:
        ccid = (r.get("ccid") or "").strip()
        if not ccid or ccid in seen:
            continue
        seen.add(ccid)
        out.append(r)
    return out


# ----------------------------
# Main
# ----------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--auth-context", required=True, help="Path to out/auth_context.json")
    ap.add_argument("--out-dir", required=True, help="Output directory (e.g. .\\out)")
    ap.add_argument("--out-csv", default="", help="Override output CSV path")
    ap.add_argument("--out-json", default="", help="Override output raw JSON path (empty disables)")
    ap.add_argument("--user-agent", default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145 Safari/537.36")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--attempts", type=int, default=5)
    ap.add_argument("--backoff-base", type=float, default=1.0)
    ap.add_argument("--backoff-cap", type=float, default=20.0)
    ap.add_argument("--sort", default="OrdCh%3DASC", help="Sort value (will be URL-encoded by requests)")
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    out_csv = args.out_csv.strip() or os.path.join(out_dir, "allchannels_map.csv")
    out_json = args.out_json.strip() or os.path.join(out_dir, "allchannels_raw.json")

    ensure_parent_dir(out_csv)
    if out_json:
        ensure_parent_dir(out_json)

    ctx = load_auth_context(args.auth_context)
    bearer = bearer_from_context(ctx)
    cookies_list = cookies_from_context(ctx)
    client_context = get_client_context_from_template(ctx)

    sess = build_session(bearer, cookies_list, args.user_agent)

    # IMPORTANT:
    # captured HAR used sort=OrdCh%253DASC (double-encoded '=')
    # if we set sort="OrdCh%3DASC", requests will encode '%' -> '%25' producing OrdCh%253DASC
    params = {
        "sort": args.sort,
        "clientContext": client_context,
    }

    log(f"Fetching AllChannels: {ALLCHANNELS_URL}")
    log(f"Params: sort={params['sort']} clientContext=(len {len(client_context)})")

    j = safe_get_json(
        sess=sess,
        url=ALLCHANNELS_URL,
        params=params,
        timeout=args.timeout,
        attempts=args.attempts,
        backoff_base=args.backoff_base,
        backoff_cap=args.backoff_cap,
    )

    if out_json:
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(j, f, indent=2)
        log(f"Wrote raw JSON: {out_json}")

    channel_list = find_best_channel_list(j)
    if not channel_list:
        err("Could not locate channel list in AllChannels JSON. Raw JSON was saved for inspection.")
        return 2

    rows = [normalize_channel_row(d) for d in channel_list if isinstance(d, dict)]
    # Keep only rows with ccid at minimum
    rows = [r for r in rows if (r.get("ccid") or "").strip()]
    rows = dedup_by_ccid(rows)

    # Light warning if resourceId is missing across the board (deeplinks won't work)
    missing_guid = sum(1 for r in rows if not (r.get("resourceId") or "").strip())
    if rows and missing_guid == len(rows):
        warn("resourceId missing for all rows. Deeplink generation needs the UUID field from AllChannels.")
    else:
        log(f"resourceId present: {len(rows) - missing_guid}/{len(rows)}")

    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ccid", "channelNumber", "callSign", "channelName", "logoUrl", "resourceId"])
        w.writeheader()
        for r in rows:
            w.writerow({k: (r.get(k) or "") for k in w.fieldnames})

    log(f"Wrote CSV: {out_csv} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
