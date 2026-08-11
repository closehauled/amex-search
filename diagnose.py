#!/usr/bin/env python
"""Quick diagnostic: load one Amex referral URL and dump what Selenium sees."""

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
import os
import time

CHROMIUM_BINARY = "/usr/bin/chromium-browser"
CHROMEDRIVER_PATH = "/usr/lib/chromium-browser/chromedriver"

# Read the referral link from the gitignored amex-referrals.txt rather than
# hardcoding it. The ref= code is personally identifying (it maps to the
# referring cardmember), so it must never be committed. See
# amex-referrals.txt.example for the file format.
REFERRALS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "amex-referrals.txt")


def first_referral_url():
    try:
        with open(REFERRALS) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except FileNotFoundError:
        pass
    raise SystemExit(
        "No referral link found. Copy amex-referrals.txt.example to "
        "amex-referrals.txt and add at least one real link."
    )


URL = first_referral_url()

def build_driver():
    opts = webdriver.ChromeOptions()
    opts.add_argument("--incognito")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.binary_location = CHROMIUM_BINARY
    service = Service(CHROMEDRIVER_PATH)
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(30)
    return driver

print("=== Building WebDriver ===")
driver = build_driver()

print(f"\n=== Loading URL: {URL} ===")
try:
    driver.get(URL)
except Exception as e:
    print(f"Page load error: {e}")

print(f"\n=== Current URL: {driver.current_url} ===")
print(f"=== Page title: {driver.title} ===")

# Wait for JS to render
print("\n=== Waiting 10s for JS rendering ===")
time.sleep(10)

print(f"\n=== Current URL after wait: {driver.current_url} ===")
print(f"=== Page title after wait: {driver.title} ===")

# Check navigator.webdriver property
webdriver_flag = driver.execute_script("return navigator.webdriver")
print(f"\n=== navigator.webdriver = {webdriver_flag} ===")

# Check page source length
source = driver.page_source
print(f"\n=== Page source length: {len(source)} chars ===")

# Show first 2000 chars of page source
print(f"\n=== First 2000 chars of page source ===")
print(source[:2000])

# Look for key elements
print("\n=== Element search ===")
for selector in [
    '[class*="offerTitle"]',
    '[class*="offer"]',
    '[class*="welcome"]',
    '[class*="points"]',
    '[class*="reward"]',
    'h1', 'h2', 'h3',
    '[data-testid]',
    '#root',
    '#app',
    'body > div',
]:
    elements = driver.find_elements(By.CSS_SELECTOR, selector)
    if elements:
        texts = [e.text[:100] for e in elements[:5] if e.text.strip()]
        print(f"  {selector}: {len(elements)} found, texts={texts}")
    else:
        print(f"  {selector}: NONE")

# Dump full visible text
body_text = driver.find_element(By.TAG_NAME, "body").text
print(f"\n=== Body text ({len(body_text)} chars) ===")
print(body_text[:3000] if body_text else "(EMPTY)")

driver.quit()
print("\n=== Done ===")
