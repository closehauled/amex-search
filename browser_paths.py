#!/usr/bin/env python3
"""Locate a Chromium-family browser, and a matching chromedriver, on any OS.

Only the two legacy Selenium scripts (`amex-300k-6.py`, `diagnose.py`) and the
`vm-tools/webapp` launcher need this. Everything else drives the browser
through Playwright, which downloads and tracks its own binary and so never has
to guess.

The search is layered, cheapest and most explicit first:

  1. An environment override (AMEX_CHROMIUM / AMEX_CHROMEDRIVER). Always wins,
     and is validated, so a typo reports itself instead of failing later inside
     selenium.
  2. PATH, over every name a Chromium build ships under.
  3. Per-platform install locations that are deliberately NOT on PATH: macOS
     .app bundles, /opt/google/chrome, flatpak exports, the Windows Program
     Files layout.
  4. The Chromium that Playwright already installed for this repo, which on a
     working setup is the one binary guaranteed to exist.

For the driver there is a fifth layer: returning None, which tells the caller
to let Selenium Manager (selenium >= 4.6) download a driver matching whichever
browser we found. That is the correct answer far more often than any path
guess, so it is a real result and not a failure.

Run as a script to print a path:  python3 browser_paths.py --chromium
"""

import glob
import os
import shutil
import subprocess
import sys

WINDOWS = sys.platform.startswith("win")
MACOS = sys.platform == "darwin"

# Every name a Chromium-family browser is invoked by. Order is preference:
# real Chromium first (what the scripts were written against), then Chrome,
# then the other Chromium forks, which work but are less tested here.
BROWSER_NAMES = (
    ["chromium.exe", "chrome.exe", "msedge.exe", "brave.exe"]
    if WINDOWS
    else [
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "chrome",
        "brave-browser",
        "microsoft-edge",
        "microsoft-edge-stable",
    ]
)

DRIVER_NAMES = ["chromedriver.exe"] if WINDOWS else ["chromedriver"]


def _expand(paths):
    """Expand ~ and environment variables, and drop anything that does not
    resolve. Windows entries reference %ProgramFiles% and friends, which are
    unset on other platforms and would otherwise expand to a literal."""
    out = []
    for p in paths:
        p = os.path.expandvars(os.path.expanduser(p))
        if "%" not in p and "$" not in p:
            out.append(p)
    return out


def _browser_dirs():
    if MACOS:
        return _expand([
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "~/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/opt/homebrew/bin/chromium",
            "/usr/local/bin/chromium",
        ])
    if WINDOWS:
        return _expand([
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
            r"%LOCALAPPDATA%\Chromium\Application\chrome.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
        ])
    return _expand([
        # Snap puts a shim on PATH, but only sometimes; the unwrapped binary is
        # here either way. Ubuntu 24.04 ships chromium as a snap and nothing
        # else, which is what broke the old hardcoded /usr/bin path.
        "/snap/bin/chromium",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/lib/chromium/chromium",
        "/usr/lib/chromium-browser/chromium-browser",
        "/opt/google/chrome/chrome",
        "/opt/google/chrome/google-chrome",
        "/opt/microsoft/msedge/msedge",
        "/opt/brave.com/brave/brave-browser",
        "/var/lib/flatpak/exports/bin/org.chromium.Chromium",
        "~/.local/share/flatpak/exports/bin/org.chromium.Chromium",
    ])


def _driver_dirs(browser=None):
    candidates = []
    # A snap-confined Chromium can only be driven by the chromedriver inside
    # the same snap: an external driver launches the browser but cannot see it
    # across the confinement boundary, and the session dies with a bare
    # "cannot connect to chrome". So when the browser came from snap, that
    # driver is checked ahead of everything else.
    if browser and browser.startswith("/snap/"):
        candidates.append("/snap/bin/chromium.chromedriver")
    if MACOS:
        candidates += ["/opt/homebrew/bin/chromedriver", "/usr/local/bin/chromedriver"]
    elif WINDOWS:
        candidates += [r"%LOCALAPPDATA%\chromedriver\chromedriver.exe"]
    else:
        candidates += [
            "/snap/bin/chromium.chromedriver",
            "/usr/bin/chromedriver",
            "/usr/local/bin/chromedriver",
            "/usr/lib/chromium/chromedriver",
            "/usr/lib/chromium-browser/chromedriver",
        ]
    return _expand(candidates)


def _playwright_chromium():
    """The browser Playwright installed for this repo. Globbed first because it
    is fast and offline; the Playwright API is asked only if the glob misses,
    since starting it spawns a node process."""
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not root:
        if MACOS:
            root = os.path.expanduser("~/Library/Caches/ms-playwright")
        elif WINDOWS:
            root = os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright")
        else:
            root = os.path.expanduser("~/.cache/ms-playwright")
    # Playwright's layout moves between versions: chrome-linux became
    # chrome-linux64, and the macOS bundle went from Chromium.app/.../Chromium
    # to "Google Chrome for Testing.app/.../Google Chrome for Testing". These
    # glob the shapes rather than any one version's exact names.
    patterns = [
        os.path.join(root, "chromium-*", "chrome-linux*", "chrome"),
        os.path.join(root, "chromium-*", "chrome-mac*", "*.app",
                     "Contents", "MacOS", "*"),
        os.path.join(root, "chromium-*", "chrome-win*", "chrome.exe"),
    ]
    hits = []
    for pat in patterns:
        hits += [h for h in glob.glob(pat) if os.access(h, os.X_OK)]
    if hits:
        # Newest install wins. Sorted on the revision as an integer, since
        # these are numbered and a string sort puts chromium-999 above
        # chromium-1224.
        def revision(path):
            for part in path.split(os.sep):
                if part.startswith("chromium-"):
                    tail = part.split("-", 1)[1]
                    return int(tail) if tail.isdigit() else -1
            return -1
        return sorted(hits, key=revision)[-1]

    # Last resort: ask Playwright itself, which is authoritative for layouts the
    # globs above do not know about. Run in a subprocess: tearing the sync API
    # down mid-import spews "Task was destroyed but it is pending!" and a
    # TargetClosedError onto stderr, which would land immediately after our own
    # error message and read like a crash. In a subprocess that noise is
    # discarded with the rest of its stderr.
    probe = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p: print(p.chromium.executable_path)\n"
    )
    try:
        out = subprocess.run([sys.executable, "-c", probe],
                             capture_output=True, text=True, timeout=60)
        path = out.stdout.strip()
        if path and os.path.exists(path):
            return path
    except Exception:
        pass
    return None


def _from_env(var):
    override = os.environ.get(var)
    if not override:
        return None
    if not os.path.exists(override):
        raise SystemExit(
            f"{var} is set to {override!r}, which does not exist. Fix it or "
            f"unset it to fall back to the normal search.")
    return override


def find_chromium(required=True):
    """Return a path to a Chromium-family browser, or None (or exit) if the
    machine has none."""
    found = _from_env("AMEX_CHROMIUM")
    if found:
        return found
    for name in BROWSER_NAMES:
        p = shutil.which(name)
        if p:
            return p
    for p in _browser_dirs():
        if os.path.exists(p):
            return p
    p = _playwright_chromium()
    if p:
        return p
    if not required:
        return None
    raise SystemExit(
        "No Chromium-family browser found. Install one (on Ubuntu: "
        "'sudo snap install chromium'; on macOS: 'brew install --cask "
        "chromium'), or run 'playwright install chromium' inside this repo's "
        "virtualenv, or set AMEX_CHROMIUM to the browser's full path.")


def find_chromedriver(browser=None):
    """Return a chromedriver path, or None to mean 'let Selenium Manager fetch
    one'. None is a normal result, not an error."""
    found = _from_env("AMEX_CHROMEDRIVER")
    if found:
        return found
    if browser and browser.startswith("/snap/"):
        snap_driver = "/snap/bin/chromium.chromedriver"
        if os.path.exists(snap_driver):
            return snap_driver
    for name in DRIVER_NAMES:
        p = shutil.which(name)
        if p:
            return p
    for p in _driver_dirs(browser):
        if os.path.exists(p):
            return p
    return None


def build_chrome_service(browser=None):
    """Build a selenium Service, letting Selenium Manager supply the driver
    when the machine has none installed. Imported lazily so this module stays
    usable (and runnable as a CLI) without selenium present."""
    from selenium.webdriver.chrome.service import Service

    path = find_chromedriver(browser)
    if path:
        return Service(path)

    try:
        from selenium import __version__ as sel_version
        major, minor = (int(x) for x in sel_version.split(".")[:2])
        if (major, minor) < (4, 6):
            raise SystemExit(
                f"No chromedriver found, and selenium {sel_version} is too old "
                "to download one itself. Either 'pip install -U selenium' "
                "(>= 4.6 manages drivers automatically) or install chromedriver "
                "and set AMEX_CHROMEDRIVER to it.")
    except (ImportError, ValueError):
        pass
    return Service()


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else "--chromium"
    if what in ("--chromium", "--browser"):
        print(find_chromium())
    elif what == "--chromedriver":
        path = find_chromedriver(find_chromium(required=False))
        if not path:
            raise SystemExit(
                "No chromedriver on this machine. Selenium >= 4.6 will "
                "download one on demand, so the Python scripts are fine; this "
                "only matters if you needed the path itself.")
        print(path)
    else:
        raise SystemExit(f"usage: {sys.argv[0]} [--chromium|--chromedriver]")


if __name__ == "__main__":
    main()
