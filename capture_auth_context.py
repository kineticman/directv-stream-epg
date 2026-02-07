#!/usr/bin/env python3
"""
capture_auth_context_selenium.py

Selenium-based auth capture for DirecTV Stream.
Fallback for when Playwright doesn't work in container environments.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlunparse

# Network request markers
ALLCHANNELS_MARKER = "/discovery/metadata/channel/v5/service/allchannels"
CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def main() -> int:
    import argparse
    
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-path", required=True)
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--auto-login", action="store_true", default=True)
    ap.add_argument("--browser", default="firefox")
    args = ap.parse_args()

    out_path = Path(args.out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Get credentials
    username = os.getenv("DTV_EMAIL", "") or os.getenv("DTV_USERNAME", "")
    password = os.getenv("DTV_PASSWORD", "")

    if not username or not password:
        print("ERROR: Set DTV_EMAIL/DTV_USERNAME and DTV_PASSWORD")
        return 1

    print(f"[INFO] Using Selenium with {args.browser}")
    print(f"[INFO] Headless: {args.headless}")

    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException
    except ImportError:
        print("ERROR: selenium not installed. Run: pip install selenium")
        return 1

    # Setup driver
    if args.browser == "firefox":
        from selenium.webdriver.firefox.options import Options
        from selenium.webdriver.firefox.service import Service
        
        options = Options()
        if args.headless:
            options.add_argument("--headless")
        options.set_preference("general.useragent.override", CHROME_UA)
        
        driver = webdriver.Firefox(options=options)
    else:
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        
        options = Options()
        if args.headless:
            options.add_argument("--headless=new")
        options.add_argument(f"--user-agent={CHROME_UA}")
        options.add_argument("--disable-blink-features=AutomationControlled")
        
        # Enable performance logging to capture network requests
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        
        driver = webdriver.Chrome(options=options)

    captured_auth = None
    use_cdp = args.browser != "firefox"  # CDP only works with Chrome
    
    # Enable network capture for Chrome
    if use_cdp:
        try:
            driver.execute_cdp_cmd("Network.enable", {})
            print("[DEBUG] Network capture enabled via CDP")
        except Exception as e:
            print(f"[WARNING] CDP not available: {e}")
            use_cdp = False
    
    def check_requests():
        nonlocal captured_auth
        if not use_cdp:
            return False
        try:
            logs = driver.get_log("performance")
            print(f"[DEBUG] Checking {len(logs)} performance log entries...")
            
            for entry in logs:
                log = json.loads(entry["message"])["message"]
                if log["method"] == "Network.requestWillBeSent":
                    url = log["params"]["request"]["url"]
                    
                    # Debug: show some requests
                    if "api.cld.dtvce.com" in url or "directv" in url:
                        print(f"[DEBUG] Saw request: {url[:80]}...")
                    
                    if ALLCHANNELS_MARKER in url and not captured_auth:
                        headers = log["params"]["request"]["headers"]
                        auth = headers.get("Authorization", headers.get("authorization", ""))
                        
                        if not auth:
                            print(f"[WARNING] Found allchannels request but no Authorization header")
                            continue
                        
                        parsed = urlparse(url)
                        params = {k: v[0] if isinstance(v, list) else v 
                                 for k, v in parse_qs(parsed.query).items()}
                        
                        captured_auth = {
                            "authorization": auth.replace("Bearer ", "").strip(),
                            "headers": headers,
                            "cookies": [{"name": c["name"], "value": c["value"], 
                                       "domain": c.get("domain", ""), "path": c.get("path", "/")}
                                      for c in driver.get_cookies()],
                            "request_template": {
                                "url": urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", "")),
                                "params": params,
                            }
                        }
                        print(f"[CAPTURED] Auth request!")
                        return True
        except Exception as e:
            print(f"[DEBUG] Check requests error: {e}")
        return False

    try:
        print("[INFO] Navigating to DirecTV...")
        driver.get("https://stream.directv.com/guide")
        time.sleep(3)

        current_url = driver.current_url
        
        if "identity.directv.com" in current_url:
            print("[INFO] On login page, attempting auto-login...")
            
            # Fill email
            print("[DEBUG] Waiting for email field...")
            email_field = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'input[type="email"]'))
            )
            email_field.send_keys(username)
            print(f"[INFO] Filled email")
            
            # Submit email by pressing Enter
            print("[DEBUG] Submitting email...")
            email_field.send_keys(Keys.RETURN)
            time.sleep(3)
            
            # Fill password
            print("[DEBUG] Waiting for password field...")
            pass_field = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'input[type="password"]'))
            )
            pass_field.send_keys(password)
            print(f"[INFO] Filled password")
            
            # Submit password by pressing Enter
            print("[DEBUG] Submitting password...")
            pass_field.send_keys(Keys.RETURN)
            print("[INFO] Login submitted")
            
            # Wait for redirect/login
            time.sleep(5)
            
            # Check for auth capture
            check_requests()
            
            # Navigate to guide if needed
            if not captured_auth:
                print("[INFO] Navigating to guide...")
                driver.get("https://stream.directv.com/guide")
                time.sleep(5)
            
        else:
            print("[INFO] Already logged in")
            time.sleep(3)
        
        # Wait for auth capture
        print("[INFO] Waiting for auth capture...")
        
        if use_cdp:
            # Use CDP to capture requests (Chrome only)
            for _ in range(60):
                if check_requests():
                    break
                time.sleep(0.5)
        else:
            # Firefox fallback: extract from cookies/localStorage after successful login
            print("[INFO] Firefox mode - extracting auth from page...")
            time.sleep(5)  # Give page time to load
            
            try:
                # Try to extract bearer token from localStorage
                local_storage = driver.execute_script("return window.localStorage;")
                session_storage = driver.execute_script("return window.sessionStorage;")
                
                # Look for auth tokens in storage
                auth_token = None
                for storage in [local_storage, session_storage]:
                    if storage:
                        for key, value in storage.items():
                            if "token" in key.lower() or "auth" in key.lower():
                                print(f"[DEBUG] Found {key} in storage")
                                if "bearer" in str(value).lower() or len(str(value)) > 50:
                                    auth_token = str(value)
                                    break
                
                if not auth_token:
                    print("[ERROR] Could not extract auth token from storage")
                    print("[INFO] Try using Chrome instead: --browser chromium")
                    return 1
                
                # Build auth context manually
                parsed = urlparse("https://api.cld.dtvce.com/discovery/metadata/channel/v5/service/allchannels")
                captured_auth = {
                    "authorization": auth_token.replace("Bearer ", "").strip(),
                    "headers": {
                        "Authorization": f"Bearer {auth_token}",
                        "User-Agent": CHROME_UA,
                    },
                    "cookies": [{"name": c["name"], "value": c["value"], 
                               "domain": c.get("domain", ""), "path": c.get("path", "/")}
                              for c in driver.get_cookies()],
                    "request_template": {
                        "url": str(parsed.scheme) + "://" + str(parsed.netloc) + str(parsed.path),
                        "params": {"sort": "OrdCh%3DASC"},  # Default params
                    }
                }
                print(f"[CAPTURED] Extracted auth from storage")
                
            except Exception as e:
                print(f"[ERROR] Storage extraction failed: {e}")
                return 1
        
        if not captured_auth:
            print("[ERROR] No auth captured")
            print(f"[DEBUG] Current URL: {driver.current_url}")
            return 1
        
        # Write output
        out_path.write_text(json.dumps(captured_auth, indent=2))
        print(f"[SUCCESS] Wrote: {out_path}")
        return 0
        
    finally:
        driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
