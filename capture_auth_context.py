#!/usr/bin/env python3
"""
capture_auth_context.py

Selenium-based auth capture for DirecTV Stream.
Logs into DirecTV, waits for the guide to load, and captures
the Authorization header + request template from network traffic.
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

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _now() -> str:
    return time.strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{_now()}] {msg}", flush=True)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--out-path", required=True)
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--auto-login", action="store_true", default=True)
    ap.add_argument("--browser", default="chromium")
    ap.add_argument("--screenshot-dir", default="",
                    help="Dir to save debug screenshots (default: same dir as --out-path)")
    ap.add_argument("--no-screenshots", action="store_true",
                    help="Disable debug screenshots")
    args = ap.parse_args()

    out_path = Path(args.out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_dir = Path(args.screenshot_dir) if args.screenshot_dir else out_path.parent
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    # Get credentials
    username = os.getenv("DTV_EMAIL", "") or os.getenv("DTV_USERNAME", "")
    password = os.getenv("DTV_PASSWORD", "")

    if not username or not password:
        log("ERROR: Set DTV_EMAIL/DTV_USERNAME and DTV_PASSWORD environment variables")
        return 1

    log(f"Browser: {args.browser} | Headless: {args.headless}")
    log(f"Username: {username[:3]}***{username[-4:]}")

    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException, NoSuchElementException
    except ImportError:
        log("ERROR: selenium not installed. Run: pip install selenium")
        return 1

    # ── Build Chrome options ──────────────────────────────────────────────
    if args.browser == "firefox":
        from selenium.webdriver.firefox.options import Options
        options = Options()
        if args.headless:
            options.add_argument("--headless")
        options.set_preference("general.useragent.override", CHROME_UA)
        driver = webdriver.Firefox(options=options)
        use_cdp = False
    else:
        from selenium.webdriver.chrome.options import Options
        options = Options()
        if args.headless:
            options.add_argument("--headless=new")

        # Required for Docker containers
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")

        # Anti-detection
        options.add_argument(f"--user-agent={CHROME_UA}")
        options.add_argument("--disable-blink-features=AutomationControlled")

        # Performance logging for network capture
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

        driver = webdriver.Chrome(options=options)
        use_cdp = True

    # ── Network capture state ─────────────────────────────────────────────
    captured_auth = None

    if use_cdp:
        try:
            driver.execute_cdp_cmd("Network.enable", {})
            log("CDP Network capture enabled")
        except Exception as e:
            log(f"WARNING: CDP not available: {e}")
            use_cdp = False

    def save_screenshot(name: str) -> None:
        if args.no_screenshots:
            return
        try:
            path = screenshot_dir / f"debug_{name}.png"
            driver.save_screenshot(str(path))
            log(f"Screenshot saved: {path}")
        except Exception as e:
            log(f"Screenshot failed: {e}")

    def check_network_logs() -> bool:
        """Scan Chrome performance logs for the allchannels API request."""
        nonlocal captured_auth
        if not use_cdp or captured_auth:
            return captured_auth is not None
        try:
            logs = driver.get_log("performance")
            if logs:
                log(f"Scanning {len(logs)} network events...")

            for entry in logs:
                msg = json.loads(entry["message"])["message"]
                if msg["method"] != "Network.requestWillBeSent":
                    continue

                url = msg["params"]["request"]["url"]

                # Show interesting requests
                if any(m in url for m in ["api.cld.dtvce.com", "stream.directv.com"]):
                    log(f"  API: {url[:120]}...")

                if ALLCHANNELS_MARKER in url:
                    headers = msg["params"]["request"]["headers"]
                    auth = headers.get("Authorization", headers.get("authorization", ""))

                    if not auth:
                        log("  Found allchannels request but no Authorization header, skipping")
                        continue

                    parsed = urlparse(url)
                    params = {
                        k: v[0] if isinstance(v, list) else v
                        for k, v in parse_qs(parsed.query).items()
                    }

                    captured_auth = {
                        "authorization": auth.replace("Bearer ", "").strip(),
                        "headers": dict(headers),
                        "cookies": [
                            {
                                "name": c["name"],
                                "value": c["value"],
                                "domain": c.get("domain", ""),
                                "path": c.get("path", "/"),
                            }
                            for c in driver.get_cookies()
                        ],
                        "request_template": {
                            "scheme": parsed.scheme,
                            "netloc": parsed.netloc,
                            "path": parsed.path,
                            "url": urlunparse(
                                (parsed.scheme, parsed.netloc, parsed.path, "", "", "")
                            ),
                            "params": params,
                        },
                    }
                    log("✅ CAPTURED auth from allchannels request!")
                    return True

        except Exception as e:
            log(f"Network log scan error: {e}")
        return False

    # ── Main flow ─────────────────────────────────────────────────────────
    try:
        driver.set_page_load_timeout(60)

        # Step 1: Navigate to guide
        log("Navigating to https://stream.directv.com/guide ...")
        driver.get("https://stream.directv.com/guide")
        time.sleep(4)

        current_url = driver.current_url
        log(f"Current URL: {current_url}")
        save_screenshot("01_initial_page")

        # Step 2: Login if needed
        if "identity.directv.com" in current_url:
            log("On login page — attempting login...")

            # ── Find and fill email/username field ────────────────────────
            email_selectors = [
                'input[type="email"]',
                'input[type="text"]',
                'input[name="email"]',
                'input[name="username"]',
                'input[name="userId"]',
                'input[id="email"]',
                'input[id="userName"]',
                'input[id="userId"]',
                'input[autocomplete="username"]',
                'input[autocomplete="email"]',
            ]

            email_field = None
            for sel in email_selectors:
                try:
                    email_field = WebDriverWait(driver, 3).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                    )
                    log(f"Found email field: {sel}")
                    break
                except TimeoutException:
                    continue

            if not email_field:
                # Last resort: find any visible input
                try:
                    inputs = driver.find_elements(
                        By.CSS_SELECTOR, "input:not([type='hidden'])"
                    )
                    visible = [i for i in inputs if i.is_displayed()]
                    log(f"Fallback: found {len(visible)} visible inputs")
                    for i, inp in enumerate(visible):
                        itype = inp.get_attribute("type") or "?"
                        iname = inp.get_attribute("name") or "?"
                        iid = inp.get_attribute("id") or "?"
                        log(f"  input[{i}]: type={itype} name={iname} id={iid}")
                    if visible:
                        email_field = visible[0]
                        log("Using first visible input")
                except Exception as e:
                    log(f"Input scan error: {e}")

            if not email_field:
                save_screenshot("02_no_email_field")
                log("ERROR: Could not find email/username input field")
                log(f"Page title: {driver.title}")
                return 1

            email_field.clear()
            email_field.send_keys(username)
            log("Filled email/username")
            save_screenshot("03_email_filled")

            # Submit email — try button first, then Enter key
            submitted = _try_submit(driver, email_field)
            log(f"Email submitted via: {'button' if submitted else 'Enter key'}")

            time.sleep(4)
            save_screenshot("04_after_email_submit")
            log(f"URL after email submit: {driver.current_url}")

            # ── Find and fill password field ──────────────────────────────
            pass_selectors = [
                'input[type="password"]',
                'input[name="password"]',
                'input[id="password"]',
                'input[autocomplete="current-password"]',
            ]

            pass_field = None
            for sel in pass_selectors:
                try:
                    pass_field = WebDriverWait(driver, 12).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                    )
                    log(f"Found password field: {sel}")
                    break
                except TimeoutException:
                    continue

            if not pass_field:
                save_screenshot("05_no_password_field")
                # Maybe we're already past login?
                if "stream.directv.com" in driver.current_url:
                    log("Already redirected past login (no password needed)")
                else:
                    log("ERROR: Could not find password field")
                    log(f"URL: {driver.current_url}")
                    # Dump visible inputs
                    try:
                        inputs = driver.find_elements(
                            By.CSS_SELECTOR, "input:not([type='hidden'])"
                        )
                        for inp in inputs:
                            if inp.is_displayed():
                                log(f"  visible input: type={inp.get_attribute('type')} "
                                    f"name={inp.get_attribute('name')} "
                                    f"id={inp.get_attribute('id')}")
                    except Exception:
                        pass
                    return 1

            if pass_field:
                pass_field.clear()
                pass_field.send_keys(password)
                log("Filled password")

                submitted = _try_submit(driver, pass_field)
                log(f"Password submitted via: {'button' if submitted else 'Enter key'}")

                time.sleep(6)
                save_screenshot("06_after_password_submit")
                log(f"URL after password submit: {driver.current_url}")

            # Check for login errors
            _check_login_errors(driver)

            # Check network logs
            check_network_logs()

            # If still on identity page, try navigating to guide
            if "identity.directv.com" in driver.current_url and not captured_auth:
                log("Still on login page — trying to navigate to guide...")
                driver.get("https://stream.directv.com/guide")
                time.sleep(6)
                save_screenshot("07_retry_guide")
                log(f"URL after retry: {driver.current_url}")

                if "identity.directv.com" in driver.current_url:
                    save_screenshot("08_login_failed")
                    log("ERROR: Login failed — still on identity page")
                    log("Check your DTV_EMAIL and DTV_PASSWORD credentials")
                    return 1

        else:
            log("Already logged in (not on identity page)")
            time.sleep(3)

        # Step 3: Wait for allchannels API call
        if not captured_auth:
            if "stream.directv.com" in driver.current_url and "/guide" not in driver.current_url:
                log("Navigating to guide page...")
                driver.get("https://stream.directv.com/guide")
                time.sleep(5)

            log("Waiting for auth capture (up to 60s)...")
            for i in range(120):
                if check_network_logs():
                    break
                time.sleep(0.5)
                if i > 0 and i % 30 == 0:
                    elapsed = i // 2
                    log(f"  Still waiting... ({elapsed}s) URL: {driver.current_url}")
                    save_screenshot(f"09_waiting_{elapsed}s")

        # Step 4: Fallback to storage extraction
        if not captured_auth:
            save_screenshot("10_no_auth_trying_storage")
            log("CDP capture failed — trying localStorage/sessionStorage fallback...")
            try:
                token = _extract_token_from_storage(driver)
                if token:
                    captured_auth = _build_auth_from_token(token, driver)
                    log("✅ Extracted auth token from browser storage!")
            except Exception as e:
                log(f"Storage extraction failed: {e}")

        if not captured_auth:
            save_screenshot("11_final_failure")
            log("ERROR: All capture methods failed")
            log(f"Final URL: {driver.current_url}")
            return 1

        # Step 5: Write output
        out_path.write_text(json.dumps(captured_auth, indent=2))
        log(f"✅ Wrote auth context: {out_path}")
        return 0

    except Exception as e:
        log(f"FATAL ERROR: {e}")
        try:
            save_screenshot("99_fatal_error")
        except Exception:
            pass
        raise

    finally:
        try:
            driver.quit()
        except Exception:
            pass


def _try_submit(driver, field) -> bool:
    """Try clicking a submit button near the field. Returns True if button found, else sends Enter."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.common.exceptions import NoSuchElementException

    btn_selectors = [
        'button[type="submit"]',
        'input[type="submit"]',
        'button[data-testid="submit"]',
        "button.submit",
        "#submitBtn",
        "#nextBtn",
    ]
    for bsel in btn_selectors:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, bsel)
            if btn.is_displayed():
                btn.click()
                return True
        except NoSuchElementException:
            continue

    field.send_keys(Keys.RETURN)
    return False


def _check_login_errors(driver) -> None:
    """Log any visible error messages on the login page."""
    from selenium.webdriver.common.by import By

    try:
        error_sels = ".error-message, .alert-danger, [role='alert'], .form-error, .error, .err-msg"
        for el in driver.find_elements(By.CSS_SELECTOR, error_sels):
            if el.is_displayed() and el.text.strip():
                log(f"⚠️ Login error on page: {el.text.strip()}")
    except Exception:
        pass


def _extract_token_from_storage(driver) -> str | None:
    """Try to find a bearer token in localStorage or sessionStorage."""
    for storage_name, js in [
        ("localStorage", "return Object.entries(window.localStorage)"),
        ("sessionStorage", "return Object.entries(window.sessionStorage)"),
    ]:
        try:
            entries = driver.execute_script(js)
            if not entries:
                continue
            for key, value in entries:
                key_lower = key.lower()
                if any(t in key_lower for t in ["token", "auth", "bearer", "access"]):
                    log(f"  Found {storage_name} key: {key} (len={len(str(value))})")
                    val = str(value).strip()
                    if len(val) > 50 and "." in val:
                        return val
                    try:
                        parsed = json.loads(val)
                        if isinstance(parsed, dict):
                            for tk in ("access_token", "accessToken", "token", "bearer"):
                                if tk in parsed and len(str(parsed[tk])) > 50:
                                    return str(parsed[tk])
                    except (json.JSONDecodeError, TypeError):
                        pass
        except Exception as e:
            log(f"  {storage_name} scan error: {e}")
    return None


def _build_auth_from_token(token: str, driver) -> dict:
    """Build an auth_context dict from a token extracted from storage."""
    token = token.replace("Bearer ", "").strip()
    return {
        "authorization": token,
        "headers": {
            "Authorization": f"Bearer {token}",
            "User-Agent": CHROME_UA,
        },
        "cookies": [
            {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
            }
            for c in driver.get_cookies()
        ],
        "request_template": {
            "scheme": "https",
            "netloc": "api.cld.dtvce.com",
            "path": "/discovery/metadata/channel/v5/service/allchannels",
            "url": "https://api.cld.dtvce.com/discovery/metadata/channel/v5/service/allchannels",
            "params": {"sort": "OrdCh%3DASC"},
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
