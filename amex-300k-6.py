#!/usr/bin/env python
# coding: utf-8

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, TimeoutException

import re
import subprocess
import time
import sys
import requests
import pandas as pd
import random
import os
from pathlib import Path

# ==========================
# Config / Tunables
# ==========================

OFFER_TARGET = "300,000"
CITIES = ["San_Francisco", "Los_Angeles", "Denver"]
NUM_SERVERS_PER_CITY = 10

WELCOME_OFFER_CSS_SELECTOR = '[class*="offerTitle"]'
RANDOMIZE_SERVERS = False
RANDOM_SEED = 11

NORDVPN_CONNECT_RETRIES = 2
NORDVPN_FAILURE_ABORT_THRESHOLD = 8
NORDVPN_RETRY_BACKOFF_SEC = 3

SELENIUM_PAGE_LOAD_TIMEOUT = 30
SELENIUM_ELEMENT_TIMEOUT = 15

# This used to be a pair of hardcoded Ubuntu 22.04 paths, which do not exist on
# Ubuntu 24.04 (chromium is a snap there) and never existed on macOS or
# Windows, so the script died with a raw selenium traceback. browser_paths
# searches PATH, the per-platform install locations, and the Chromium
# Playwright already installed for this repo; AMEX_CHROMIUM and
# AMEX_CHROMEDRIVER override it.
import browser_paths


CHROMIUM_BINARY = browser_paths.find_chromium()

DEFAULT_REFERRAL_URL = (
    "https://americanexpress.com/en-us/referral/business-platinum-charge-card?ref=YOUR_REF_CODE&XL=XXXXX"
)

MODE_VM_BROWSER = "vm"
MODE_EXTERNAL = "external"


# ==========================
# Helpers – ANSI styling
# ==========================

def _supports_ansi():
    return sys.stdout.isatty()

ANSI_ENABLED = _supports_ansi()

def blue(text: str) -> str:
    if not ANSI_ENABLED:
        return text
    return f"\033[94m{text}\033[0m"  # bright/deep blue

def bold(text: str) -> str:
    if not ANSI_ENABLED:
        return text
    return f"\033[1m{text}\033[0m"

def strike(text: str) -> str:
    if not ANSI_ENABLED:
        return text
    return f"\033[9m{text}\033[0m"   # strikethrough

def green(text: str) -> str:
    if not ANSI_ENABLED:
        return text
    return f"\033[92m{text}\033[0m"  # bright green

def red(text: str) -> str:
    if not ANSI_ENABLED:
        return text
    return f"\033[91m{text}\033[0m"  # bright red

def banner(title: str) -> None:
    width = 70
    bar = "=" * width
    print(blue("\n" + bar))
    print(blue(bold(title.center(width))))
    print(blue(bar))


# ==========================
# Mode selection
# ==========================

def select_mode():
    while True:
        print("Select mode:")
        print("  1) VM browser mode (fill application inside this VM)")
        print("  2) External browser mode (use this VM as scanner only)")
        choice = input("Enter 1 or 2: ").strip()
        if choice == "1":
            return MODE_VM_BROWSER
        elif choice == "2":
            return MODE_EXTERNAL
        else:
            print("Invalid choice, try again.\n")


# ==========================
# Referral URL loading
# ==========================

def load_referral_urls():
    """
    Load referral URLs from amex-referrals.txt (if present) and
    ALWAYS include the built-in DEFAULT_REFERRAL_URL at the end.
    """
    try:
        base_dir = Path(__file__).resolve().parent
    except NameError:
        base_dir = Path(os.getcwd())

    ref_file = base_dir / "amex-referrals.txt"
    urls = []

    if ref_file.exists():
        try:
            with ref_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    urls.append(line)
            print(f"[INFO] Loaded {len(urls)} referral URL(s) from {ref_file.name}")
        except Exception as e:
            print(f"[WARN] Failed to read {ref_file.name}: {e}")
    else:
        print("[INFO] amex-referrals.txt not found.")

    urls.append(DEFAULT_REFERRAL_URL)
    print(f"[INFO] Total referral URLs to test (including built-in): {len(urls)}")
    return urls


# ==========================
# NordVPN helpers
# ==========================

def run_command(cmd, use_shell=False):
    try:
        out = subprocess.check_output(
            cmd, shell=use_shell, text=True, stderr=subprocess.STDOUT
        )
        return True, out
    except subprocess.CalledProcessError as e:
        return False, e.output or str(e)
    except FileNotFoundError as e:
        return False, f"FileNotFoundError: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"


def nordvpn_disconnect_quiet():
    if "win" in sys.platform:
        nvpn_path = r"C:\Program Files\NordVPN\nordvpn.exe"
        cmd = [nvpn_path, "-d"]
        success, out = run_command(cmd, use_shell=False)
    elif "lin" in sys.platform:
        cmd = ["nordvpn", "d"]
        success, out = run_command(cmd, use_shell=False)
    else:
        return False

    if not success:
        print(f"[WARN] nordvpn disconnect issue: {out.strip()}")
        return False

    print(out.strip())
    return True


def nordvpn_connect(server, retries=NORDVPN_CONNECT_RETRIES):
    hostname_prefix = str(server.hostname.split(".")[0])
    # The prefix comes from the NordVPN API; never hand API data to a shell.
    if not re.fullmatch(r"[A-Za-z0-9-]+", hostname_prefix):
        print(f"[ERROR] refusing suspicious server prefix: {hostname_prefix!r}")
        return False

    for attempt in range(retries + 1):
        if "win" in sys.platform:
            nvpn_path = r"C:\Program Files\NordVPN\nordvpn.exe"
            cmd = [nvpn_path, "-c", "-n", hostname_prefix]
            success, out = run_command(cmd, use_shell=False)
        elif "lin" in sys.platform:
            cmd = ["nordvpn", "c", hostname_prefix]
            success, out = run_command(cmd, use_shell=False)
        else:
            print(f"[ERROR] Unsupported platform for NordVPN CLI: {sys.platform}")
            return False

        out_str = (out or "").strip()

        if success:
            print(out_str)
            return True

        lower = out_str.lower()

        if "dedicated ip subscription" in lower or "dedicated-ip" in lower:
            print(
                f"[ERROR] {hostname_prefix} appears to be a dedicated-IP server; "
                "skipping without retries."
            )
            return False

        print(
            f"[ERROR] NordVPN connect failed (attempt {attempt + 1}/{retries + 1}) "
            f"for {hostname_prefix}: {out_str}"
        )

        if attempt < retries:
            print("[INFO] Attempting disconnect + retry...")
            nordvpn_disconnect_quiet()
            time.sleep(NORDVPN_RETRY_BACKOFF_SEC)

    print(f"[ERROR] All NordVPN connect attempts failed for {hostname_prefix}")
    return False


# ==========================
# Nord API
# ==========================

def filter_out_dedicated_ip_servers(df):
    if "categories" not in df.columns:
        return df

    def is_dedicated(cats):
        if not isinstance(cats, list):
            return False
        names = [
            str(c.get("name", "")).lower()
            for c in cats
            if isinstance(c, dict)
        ]
        joined = " ".join(names)
        return ("dedicated ip" in joined) or ("dedicated_ip" in joined)

    try:
        mask = ~df["categories"].apply(is_dedicated)
        filtered = df[mask].reset_index(drop=True)
        removed = len(df) - len(filtered)
        if removed:
            print(f"[INFO] Filtered out {removed} dedicated-IP servers from list.")
        return filtered
    except Exception as e:
        print(f"[WARN] Failed to filter dedicated-IP servers from API list: {e}")
        return df


def get_server_list():
    try:
        url = "https://api.nordvpn.com/v1/servers?limit=16384"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        df = pd.DataFrame(data)
        df = filter_out_dedicated_ip_servers(df)
        return df
    except Exception as err:
        print(f"[ERROR] Unable to fetch Nord server list: {err}")
        return None


def find_servers_matching_city(df, city):
    city_str = city.replace("_", " ")
    df = df.copy()

    if "locations" not in df.columns:
        print(f"[WARN] 'locations' column missing; cannot filter by city '{city_str}'")
        return df.iloc[0:0].reset_index(drop=True)

    try:
        df["locations_str"] = df["locations"].astype("string")
        df["city_match"] = df["locations_str"].str.contains(city_str, case=False, na=False)
        matches = df[df["city_match"]].reset_index(drop=True)
        return matches
    except Exception as e:
        print(f"[ERROR] Failed to filter servers for city {city_str}: {e}")
        return df.iloc[0:0].reset_index(drop=True)


# ==========================
# Selenium helpers
# ==========================

def build_webdriver():
    chrome_options = webdriver.ChromeOptions()
    chrome_options.add_argument("--incognito")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")

    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")

    chrome_options.binary_location = CHROMIUM_BINARY
    service = browser_paths.build_chrome_service(CHROMIUM_BINARY)

    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.set_page_load_timeout(SELENIUM_PAGE_LOAD_TIMEOUT)
    return driver


def get_offer_text(driver, url):
    try:
        driver.get(url)
    except TimeoutException:
        print(f"[ERROR] Page load timed out for URL: {url}")
        return False, None
    except WebDriverException as e:
        print(f"[ERROR] WebDriver page load error for URL {url}: {e}")
        return False, None

    try:
        WebDriverWait(driver, SELENIUM_ELEMENT_TIMEOUT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, WELCOME_OFFER_CSS_SELECTOR))
        )
        elements = driver.find_elements(By.CSS_SELECTOR, WELCOME_OFFER_CSS_SELECTOR)
        if not elements:
            print("[INFO] No offer elements found after wait.")
            return False, None

        text = elements[0].text
        print(text)
        return True, text

    except TimeoutException:
        print("[ERROR] Timed out waiting for offer element.")
        return False, None
    except WebDriverException as e:
        print(f"[ERROR] WebDriver find_elements error: {e}")
        return False, None
    except Exception as e:
        print(f"[ERROR] Unexpected error extracting offer: {e}")
        return False, None


# ==========================
# Main
# ==========================

def main():
    random.seed(RANDOM_SEED)

    mode = select_mode()
    referral_urls = load_referral_urls()

    df = get_server_list()
    if df is None or df.empty:
        print("[FATAL] Could not fetch NordVPN server list. Exiting.")
        return

    offer_found = False
    vpn_connected = False
    total_nordvpn_failures = 0
    driver = None

    try:
        for city in CITIES:
            print(f"\n=== Searching city: {city} ===")

            df_city = find_servers_matching_city(df, city)
            if df_city.empty:
                print(f"[WARN] No servers found for city {city}")
                continue

            max_servers = min(NUM_SERVERS_PER_CITY, len(df_city))
            indices = list(range(max_servers))

            if RANDOMIZE_SERVERS:
                random.shuffle(indices)

            for idx in indices:
                server = df_city.iloc[idx]
                print("Connect to :", server.hostname)

                if not nordvpn_connect(server):
                    total_nordvpn_failures += 1
                    print(
                        f"[WARN] Skipping server {server.hostname} due to connection errors. "
                        f"Total failures so far: {total_nordvpn_failures}"
                    )

                    if total_nordvpn_failures >= NORDVPN_FAILURE_ABORT_THRESHOLD:
                        print("[FATAL] Too many NordVPN connection failures. Aborting.")
                        return
                    continue

                vpn_connected = True
                time.sleep(5)

                for url in referral_urls:
                    print(f"[INFO] Testing referral URL: {url}")
                    local_driver = None
                    try:
                        local_driver = build_webdriver()
                    except WebDriverException as e:
                        print(f"[ERROR] Failed to start WebDriver: {e}")
                        break

                    success, offer_text = get_offer_text(local_driver, url)

                    if not success or not offer_text:
                        try:
                            local_driver.quit()
                        except Exception as e:
                            print(f"[WARN] Error while closing WebDriver: {e}")
                        local_driver = None
                        continue

                    if OFFER_TARGET in offer_text:
                        offer_found = True

                        # ===== EXTERNAL BROWSER MODE =====
                        if mode == MODE_EXTERNAL:
                            banner("ELEVATED OFFER FOUND")

                            green_offer = green(bold("300,000 Membership Rewards® Points"))
                            decorative_line = (
                                f"{strike('200,000')}  {green_offer}"
                                if ANSI_ENABLED else "~~200,000~~ 300,000 Membership Rewards® Points"
                            )

                            print(blue(f"Offer text from page: {offer_text}"))
                            print(decorative_line)
                            print(blue(f"Referral URL: {url}"))
                            print(blue(f"City:        {city}"))
                            print(blue(f"Server:      {server.hostname}"))

                            print("=" * 70)
                            print(bold("ACTION STEPS:"))
                            print("1) KEEP NordVPN CONNECTED while you open the referral URL in your preferred browser.")
                            print("2) WAIT until the 300,000-point offer is clearly visible on the page.")
                            print(red(bold("3) DISCONNECT YOUR VPN BEFORE YOU SUBMIT THE APPLICATION.")))
                            print("4) After disconnecting, complete and submit the application.")
                            print("=" * 70 + "\n")

                            try:
                                local_driver.quit()
                            except Exception as e:
                                print(f"[WARN] Error while closing WebDriver: {e}")
                            local_driver = None
                            return  # leave VPN up; you handle browser externally

                        # ===== VM BROWSER MODE =====
                        else:
                            banner("ELEVATED OFFER FOUND (VM BROWSER MODE)")

                            green_offer = green(bold("300,000 Membership Rewards® Points"))
                            decorative_line = (
                                f"{strike('200,000')}  {green_offer}"
                                if ANSI_ENABLED else "~~200,000~~ 300,000 Membership Rewards® Points"
                            )

                            print(blue(f"Offer text from page: {offer_text}"))
                            print(decorative_line)
                            print(blue(f"Referral URL: {url}"))
                            print(blue(f"City:        {city}"))
                            print(blue(f"Server:      {server.hostname}"))
                            print(blue("=" * 70))

                            driver = local_driver
                            local_driver = None

                            try:
                                driver.execute_script(
                                    "window.focus(); "
                                    "window.scrollTo(0, 0); "
                                    "window.scrollBy(0, 400);"
                                )
                            except Exception as e:
                                print(f"[WARN] Error running initial focus/scroll JS: {e}")

                            nordvpn_disconnect_quiet()
                            vpn_connected = False

                            print("[INFO] Waiting 15 seconds for the page JS to settle before you start filling the form...")
                            time.sleep(15)

                            break

                    else:
                        try:
                            local_driver.quit()
                        except Exception as e:
                            print(f"[WARN] Error while closing WebDriver: {e}")
                        local_driver = None

                if offer_found:
                    break
                else:
                    if vpn_connected and mode != MODE_EXTERNAL:
                        nordvpn_disconnect_quiet()
                        vpn_connected = False

            if offer_found:
                break

    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user (Ctrl+C). Cleaning up...")
    except Exception as e:
        print(f"[FATAL] Unhandled error in main loop: {e}")
    finally:
        if vpn_connected and not offer_found:
            nordvpn_disconnect_quiet()
        if driver is not None and (not offer_found or mode != MODE_VM_BROWSER):
            try:
                driver.quit()
            except Exception as e:
                print(f"[WARN] Error while closing WebDriver in cleanup: {e}")

    if offer_found and mode == MODE_VM_BROWSER and driver is not None:
        try:
            confirm = ""
            while str(confirm).upper() != "COMPLETE":
                confirm = input(
                    "Please complete the application now! Once complete type COMPLETE and press ENTER to close the browser: "
                )
        except KeyboardInterrupt:
            print("\n[INFO] Interrupted while waiting for completion.")
        try:
            driver.quit()
        except Exception as e:
            print(f"[WARN] Error while closing WebDriver after completion: {e}")
    elif not offer_found:
        print("[INFO] No matching offer found. Try more servers, cities, or referral URLs.")


if __name__ == "__main__":
    main()