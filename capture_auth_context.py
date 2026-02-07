#!/usr/bin/env python3
"""
capture_auth_context.py

100% headless DirecTV Stream auth capture with auto-login.
Captures OAuth tokens and authorization headers for API access.

Usage:
    set DTV_USERNAME=your-email@example.com
    set DTV_PASSWORD=your-password
    python capture_auth_context.py --headless
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

# Network request markers
TARGET_SUBSTRING = "/right/authorization/channel/v1"
ALLCHANNELS_MARKER = "/discovery/metadata/channel/v5/service/allchannels"
TOKENGO_SUBSTRING = "/authn-tokengo/v3/tokens"


def main() -> int:
    # Load .env file if it exists
    env_file = Path(".env")
    if env_file.exists():
        print(f"[INFO] Loading .env file")
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key.strip()] = value.strip()
    
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-path", help="Output path for auth_context.json (if not set, uses --out-dir)")
    ap.add_argument("--out-dir", default="out", help="Output directory (default: out)")
    ap.add_argument("--headless", action="store_true", default=True, help="Run headless (default: True)")
    ap.add_argument("--browser", default="firefox", choices=["chromium", "firefox", "webkit"])
    ap.add_argument("--timeout", type=int, default=60, help="Timeout in seconds")
    ap.add_argument("--auto-login", action="store_true", default=True, help="Auto-login (default: True)")
    args = ap.parse_args()

    # Support both --out-path and --out-dir for backward compatibility
    if args.out_path:
        auth_context_path = Path(args.out_path)
        out_dir = auth_context_path.parent
    else:
        out_dir = Path(args.out_dir)
        auth_context_path = out_dir / "auth_context.json"
    
    out_dir.mkdir(parents=True, exist_ok=True)

    storage_state_path = out_dir / "storage_state.json"
    tokens_path = out_dir / "tokens.json"

    # Get credentials from environment (support both DTV_EMAIL and DTV_USERNAME)
    username = os.getenv("DTV_EMAIL", "") or os.getenv("DTV_USERNAME", "")
    password = os.getenv("DTV_PASSWORD", "")

    if not username or not password:
        print("ERROR: Set DTV_USERNAME and DTV_PASSWORD environment variables")
        return 1

    print(f"[INFO] Using browser: {args.browser}")
    print(f"[INFO] Headless: {args.headless}")
    print(f"[INFO] Output: {out_dir}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright not installed. Run: pip install playwright")
        print("Then: python -m playwright install firefox")
        return 1

    # Captured data
    captured_auth = None
    captured_tokens = None
    request_count = 0  # Debug counter

    def on_request(request):
        nonlocal captured_auth, request_count
        try:
            url = request.url
            request_count += 1
            
            # Debug: Log every 10th request
            if request_count % 10 == 0:
                print(f"[DEBUG] Seen {request_count} requests so far...")
            
            # Capture either playback auth or allchannels request
            if (TARGET_SUBSTRING in url or ALLCHANNELS_MARKER in url) and not captured_auth:
                headers = request.headers
                captured_auth = {
                    "url": url,
                    "authorization": headers.get("authorization", ""),
                    "headers": {k: v for k, v in headers.items() if k.lower() not in ["cookie", "host"]},
                }
                marker = "playback" if TARGET_SUBSTRING in url else "allchannels"
                print(f"[CAPTURED] Auth request ({marker}): {url[:80]}...")
        except:
            pass

    def on_response(response):
        nonlocal captured_tokens
        try:
            url = response.url
            if TOKENGO_SUBSTRING in url and not captured_tokens:
                try:
                    body = response.json()
                    captured_tokens = {
                        "access_token": body.get("access_token"),
                        "refresh_token": body.get("refresh_token"),
                        "token_type": body.get("token_type"),
                        "expires_in": body.get("expires_in"),
                    }
                    print(f"[CAPTURED] OAuth tokens")
                except:
                    pass
        except:
            pass

    with sync_playwright() as p:
        # Launch browser
        if args.browser == "firefox":
            browser = p.firefox.launch(headless=args.headless)
        elif args.browser == "webkit":
            browser = p.webkit.launch(headless=args.headless)
        else:
            browser = p.chromium.launch(
                headless=args.headless,
                args=["--disable-blink-features=AutomationControlled"]
            )

        # Chrome user agent (DirecTV only supports Chrome/Edge/Safari)
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )

        # Use existing session if available
        context_opts = {"user_agent": ua}
        if storage_state_path.exists():
            print(f"[INFO] Using saved session: {storage_state_path}")
            context_opts["storage_state"] = str(storage_state_path)
        else:
            print(f"[INFO] No saved session, will log in")

        context = browser.new_context(**context_opts)
        page = context.new_page()

        # Attach listeners
        page.on("request", on_request)
        page.on("response", on_response)

        print(f"[INFO] Navigating to DirecTV...")
        try:
            page.goto("https://stream.directv.com/guide", timeout=args.timeout * 1000)
        except Exception as e:
            print(f"[ERROR] Navigation failed: {e}")
            browser.close()
            return 1

        time.sleep(3)
        current_url = page.url

        # Check if on login page
        if "identity.directv.com" in current_url and "weblogin" in current_url:
            print(f"[INFO] On login page, attempting auto-login...")
            print(f"[DEBUG] Login page URL: {current_url}")

            try:
                # Fill email
                print(f"[DEBUG] Looking for email field...")
                email_input = page.locator('input[type="email"]').first
                email_input.wait_for(state="visible", timeout=10000)
                print(f"[DEBUG] Email field found and visible")
                
                email_input.fill(username)
                print(f"[INFO] Filled email: {username[:3]}***")
                
                # Verify email was filled
                filled_email = email_input.input_value()
                print(f"[DEBUG] Email field value after fill: {filled_email[:3]}***")

                # Press Enter to go to password page
                from playwright.sync_api import Keyboard
                print(f"[DEBUG] Pressing Enter on email field...")
                email_input.press("Enter")
                print(f"[DEBUG] Waiting for password page to load...")
                time.sleep(2)
                
                # Check what page we're on
                after_email_url = page.url
                print(f"[DEBUG] URL after email submit: {after_email_url[:80]}...")

                # Fill password
                print(f"[DEBUG] Looking for password field...")
                pass_input = page.locator('input[type="password"]').first
                pass_input.wait_for(state="visible", timeout=10000)
                print(f"[DEBUG] Password field found and visible")
                
                pass_input.fill(password)
                print(f"[INFO] Filled password: ***")
                
                # Verify password was filled
                filled_pass = pass_input.input_value()
                print(f"[DEBUG] Password field has value: {bool(filled_pass)} (length: {len(filled_pass)})")

                # Try clicking the Sign In button instead of pressing Enter
                print(f"[DEBUG] Looking for Sign In button...")
                try:
                    sign_in_button = page.locator('button:has-text("Sign In"), button:has-text("Sign in"), button[type="submit"]').first
                    button_visible = sign_in_button.is_visible()
                    print(f"[DEBUG] Sign In button visible: {button_visible}")
                    
                    if button_visible:
                        sign_in_button.click()
                        print(f"[INFO] Clicked Sign In button")
                    else:
                        print(f"[WARNING] Button exists but not visible, pressing Enter instead")
                        pass_input.press("Enter")
                        print(f"[INFO] Pressed Enter on password")
                except Exception as btn_error:
                    # Fallback to pressing Enter
                    print(f"[WARNING] Could not find/click button: {btn_error}")
                    pass_input.press("Enter")
                    print(f"[INFO] Pressed Enter on password (fallback)")
                
                print(f"[INFO] Submitted login")

                # Check if still on login page after a moment
                time.sleep(3)
                check_url = page.url
                print(f"[DEBUG] URL after login attempt: {check_url[:80]}...")
                
                if "identity.directv.com" in check_url and "weblogin" in check_url:
                    print(f"[WARNING] Still on login page after submission - login may have failed")
                    # Try to see if there's an error message
                    try:
                        error_elem = page.locator('[role="alert"], .error, .error-message').first
                        if error_elem.is_visible():
                            error_text = error_elem.inner_text()
                            print(f"[ERROR] Login error message: {error_text}")
                    except:
                        pass

                # Wait a bit more for login to process
                time.sleep(2)
                
                # Explicitly navigate to guide page (the redirect might not complete properly)
                print(f"[INFO] Navigating to guide page...")
                print(f"[DEBUG] Current URL before guide nav: {page.url[:80]}...")
                try:
                    page.goto("https://stream.directv.com/guide", timeout=30000)
                    print(f"[DEBUG] Guide navigation completed")
                    time.sleep(5)  # Give the guide page time to load and make API calls
                    print(f"[DEBUG] URL after guide nav: {page.url[:80]}...")
                    
                    # Check if we're actually on the guide page
                    if "stream.directv.com/guide" in page.url:
                        print(f"[DEBUG] Successfully on guide page")
                    else:
                        print(f"[WARNING] Not on guide page, on: {page.url}")
                        
                except Exception as nav_error:
                    print(f"[WARNING] Guide navigation issue: {nav_error}")
                    print(f"[DEBUG] URL after failed nav: {page.url[:80]}...")
                
                # Now wait for auth capture
                print(f"[INFO] Waiting for auth capture...")
                print(f"[DEBUG] Request count before wait: {request_count}")
                wait_start = time.time()
                while time.time() - wait_start < 30:
                    if captured_auth:
                        print(f"[INFO] Auth captured!")
                        break
                    time.sleep(0.5)
                
                print(f"[DEBUG] Request count after wait: {request_count}")
                
                if not captured_auth:
                    print(f"[ERROR] No auth captured after navigating to guide")
                    print(f"[DEBUG] Current URL: {page.url}")
                    print(f"[DEBUG] Total requests seen: {request_count}")
                    
                    # Try to see page title
                    try:
                        page_title = page.title()
                        print(f"[DEBUG] Page title: {page_title}")
                    except:
                        pass
                    
                    browser.close()
                    return 1

                # Save session
                try:
                    context.storage_state(path=str(storage_state_path))
                    print(f"[INFO] Saved session: {storage_state_path}")
                except Exception:
                    pass

            except Exception as e:
                if captured_auth:
                    print(f"[WARNING] Errors during login but auth was captured: {e}")
                else:
                    print(f"[ERROR] Auto-login failed: {e}")
                    browser.close()
                    return 1

        elif "stream.directv.com" in current_url:
            print(f"[INFO] Already logged in")

        # Wait for auth requests
        print(f"[INFO] Waiting for auth requests...")
        print(f"[DEBUG] Total requests seen so far: {request_count}")
        timeout_at = time.time() + 30
        while time.time() < timeout_at:
            if captured_auth:  # Don't require tokens
                break
            time.sleep(0.5)
        
        print(f"[DEBUG] Final request count: {request_count}")
        if not captured_auth:
            print(f"[WARNING] No auth captured after seeing {request_count} requests total")

        # Save results
        if captured_auth:
            # Parse URL to extract template format
            from urllib.parse import urlparse, parse_qs
            
            parsed = urlparse(captured_auth["url"])
            params = parse_qs(parsed.query)
            
            # Convert multi-value params to single values
            params_single = {k: v[0] if isinstance(v, list) and v else v for k, v in params.items()}
            
            auth_output = {
                "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "authorization": captured_auth["authorization"],
                "headers": captured_auth["headers"],
                "cookies": context.cookies(),  # Add cookies for fetch_allchannels_map
                "request_template": {
                    "scheme": parsed.scheme,
                    "netloc": parsed.netloc,
                    "path": parsed.path,
                    "params": params_single,
                    "ccid_param": "ccid"  # Standard param name
                }
            }
            
            if captured_tokens:
                auth_output["tokens"] = captured_tokens

            auth_context_path.write_text(json.dumps(auth_output, indent=2))
            print(f"[SUCCESS] Wrote: {auth_context_path}")

        if captured_tokens:
            tokens_path.write_text(json.dumps(captured_tokens, indent=2))
            print(f"[SUCCESS] Wrote: {tokens_path}")

        if not captured_auth and not captured_tokens:
            print(f"[WARNING] No auth data captured - may need to refresh page")

        browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
