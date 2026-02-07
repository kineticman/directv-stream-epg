#!/usr/bin/env python3
"""
capture_auth_context.py

Interactive helper to capture DirecTV Stream "allchannels" request context
(Authorization bearer token, cookies, request URL/params, user-agent).

Supports both manual login (non-headless) and auto-login (with credentials).

Usage:
  Manual login (opens browser window):
    python capture_auth_context.py --out-path ./data/auth_context.json --no-headless
  
  Auto-login (headless, requires credentials):
    export DTV_EMAIL="your-email@example.com"
    export DTV_PASSWORD="your-password"
    python capture_auth_context.py --out-path ./data/auth_context.json --headless --auto-login

IMPORTANT: DirecTV blocks Firefox, so we always spoof Chrome user-agent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlunparse

ALLCHANNELS_MARKER = "/discovery/metadata/channel/v5/service/allchannels"

# DirecTV blocks Firefox, always use Chrome UA
CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _normalize_params(qs: dict[str, list[str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, vals in qs.items():
        if not vals:
            continue
        out[k] = vals[-1]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-path", required=True, help="Where to write auth_context.json")
    ap.add_argument("--browser", choices=["chromium", "firefox", "webkit"], default="chromium")
    head = ap.add_mutually_exclusive_group()
    head.add_argument("--headless", action="store_true", help="Run browser headless")
    head.add_argument("--no-headless", action="store_true", help="Run browser with a visible window (default)")
    ap.add_argument("--timeout", type=int, default=180, help="Seconds to wait for allchannels request")
    ap.add_argument("--auto-login", action="store_true", help="Attempt automated login (requires DTV_EMAIL and DTV_PASSWORD env vars)")
    args = ap.parse_args()

    out_path = Path(args.out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    
    storage_state_path = out_path.parent / "storage_state.json"

    # Check for credentials if auto-login requested
    email = os.environ.get("DTV_EMAIL", "").strip()
    password = os.environ.get("DTV_PASSWORD", "").strip()
    
    if args.auto_login and (not email or not password):
        print("ERROR: --auto-login requires DTV_EMAIL and DTV_PASSWORD environment variables", file=sys.stderr)
        return 2

    # Lazy import so normal runs don't require playwright until needed.
    from playwright.sync_api import sync_playwright  # type: ignore

    headless = bool(args.headless) and not bool(args.no_headless)

    print(f"[INFO] Browser: {args.browser}")
    print(f"[INFO] Headless: {headless}")
    print(f"[INFO] Auto-login: {args.auto_login}")
    if storage_state_path.exists():
        print(f"[INFO] Found saved session: {storage_state_path}")

    captured: dict[str, object] = {}

    with sync_playwright() as p:
        browser_type = getattr(p, args.browser)
        browser = browser_type.launch(headless=headless)
        
        # CRITICAL: DirecTV blocks Firefox, always spoof Chrome
        context_opts = {"user_agent": CHROME_UA}
        
        # Try to reuse saved session
        if storage_state_path.exists():
            context_opts["storage_state"] = str(storage_state_path)
        
        ctx = browser.new_context(**context_opts)
        page = ctx.new_page()

        def on_request(req):
            try:
                url = req.url
                if ALLCHANNELS_MARKER not in url:
                    return
                headers = {k.lower(): v for k, v in req.headers.items()}
                auth = headers.get("authorization", "")
                ua = headers.get("user-agent", "") or headers.get("user_agent", "")
                parsed = urlparse(url)
                params = _normalize_params(parse_qs(parsed.query))
                base_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

                captured.update({
                    "authorization": auth.replace("Bearer ", "").strip(),
                    "user_agent": ua,
                    "request_template": {
                        "url": base_url,
                        "params": params,
                    },
                })
            except Exception:
                # Don't crash the hook; just ignore
                return

        page.on("request", on_request)

        print("[INFO] Navigating to https://stream.directv.com/guide")
        page.goto("https://stream.directv.com/guide", wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        
        current_url = page.url
        
        # Check if on login page
        is_login_page = "identity.directv.com" in current_url and "weblogin" in current_url
        
        if is_login_page:
            if args.auto_login:
                print("[INFO] On login page, attempting auto-login...")
                try:
                    # Wait for email field (React app takes time to render)
                    email_field = page.locator('input[type="email"]').first
                    email_field.wait_for(state="visible", timeout=10000)
                    
                    # Fill and submit email
                    email_field.fill(email)
                    print("[INFO] Filled email")
                    email_field.press("Enter")
                    print("[INFO] Submitted email, waiting for password page...")
                    time.sleep(2)
                    
                    # Wait for password field
                    password_field = page.locator('input[type="password"]').first
                    password_field.wait_for(state="visible", timeout=10000)
                    
                    # Fill and submit password
                    password_field.fill(password)
                    print("[INFO] Filled password")
                    password_field.press("Enter")
                    print("[INFO] Submitted password, waiting for redirect...")
                    
                    # Wait for successful login (redirect to stream.directv.com)
                    page.wait_for_url("**/stream.directv.com/**", timeout=20000)
                    print("[INFO] ✓ Login successful!")
                    
                    # Save session for future use
                    ctx.storage_state(path=str(storage_state_path))
                    print(f"[INFO] Saved session: {storage_state_path}")
                    
                except Exception as e:
                    print(f"[ERROR] Auto-login failed: {e}", file=sys.stderr)
                    browser.close()
                    return 2
            else:
                print("[INFO] On login page. Please log in manually in the browser window.")
                print("[INFO] (Or use --auto-login with DTV_EMAIL and DTV_PASSWORD env vars)")
        else:
            print("[INFO] Already logged in (or not on login page)")

        # Wait for allchannels request capture
        print("[INFO] Waiting for allchannels request...")
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if captured.get("authorization") and captured.get("request_template"):
                print("[INFO] ✓ Captured allchannels request!")
                break
            page.wait_for_timeout(500)

        # Always collect cookies
        try:
            cookies = ctx.cookies()
        except Exception:
            cookies = []
        captured["cookies"] = cookies

        browser.close()

    if not captured.get("authorization"):
        print("ERROR: Did not capture Authorization header.", file=sys.stderr)
        print("Try: --no-headless to log in manually, or --auto-login with credentials", file=sys.stderr)
        return 2
    if not captured.get("request_template"):
        print("ERROR: Did not capture allchannels request template.", file=sys.stderr)
        return 2

    out_path.write_text(json.dumps(captured, indent=2), encoding="utf-8")
    print(f"[SUCCESS] Wrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
