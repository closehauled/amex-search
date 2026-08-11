#!/usr/bin/env python3
"""Find a Chromium-family browser, and a driver that can drive it, on any OS.

Only the two legacy Selenium scripts (`amex-300k-6.py`, `diagnose.py`) and the
`vm-tools/webapp` launcher need this. Everything else goes through Playwright,
which downloads and tracks its own browser and so never has to guess.

The design principle here is that guessing is unavoidable but trusting a guess
is not. Discovery collects every plausible candidate in preference order and
then *proves* one before returning it, so a binary that exists but does not
work loses to a lower-ranked one that does. That matters more than the ranking:
a stale wrapper, a half-downloaded browser, or a distro package missing a
shared library all present as a perfectly good executable right up until the
moment something tries to use it.

Candidate sources, in order:

  1. Environment overrides: AMEX_CHROMIUM first, then CHROME_BIN and
     CHROMIUM_BIN, which other tools in this ecosystem already set. Each takes
     a full path or a bare name to look up on PATH.
  2. PATH, over every name a Chromium build ships under.
  3. Per-platform install locations that are deliberately not on PATH: macOS
     .app bundles, /opt/google/chrome, snap, flatpak, and on Windows both the
     registry's App Paths and the Program Files layout.
  4. The browser Playwright installed for this repo, full build first and the
     headless shell as a last resort.

Two traps this handles that a path list alone does not:

  * Snap and flatpak put a launcher *wrapper* on PATH, not the browser. Passing
    a wrapper as binary_location breaks chromedriver, which launches it, sees
    the wrapper re-exec and exit, and reports "session not created: Chrome
    instance exited". Wrappers are resolved to the real binary where one can be
    located, and validation catches the rest.
  * A driver that cannot drive the browser it was paired with is a normal
    condition, not an exception. `start_chrome` retries with Selenium Manager
    rather than propagating the first failure.

Run it directly to see what it picks and why:

    python3 browser_paths.py --list        every candidate, ranked, with status
    python3 browser_paths.py --chromium    just the chosen browser path
    python3 browser_paths.py --selftest    prove the choice by launching it
"""

import glob
import os
import shutil
import subprocess
import sys

WINDOWS = sys.platform.startswith("win")
MACOS = sys.platform == "darwin"

# How long a candidate gets to answer `--version` before it is considered
# broken. A working browser answers in well under a second; the timeout exists
# for the pathological case of a binary that hangs, which would otherwise hang
# the tool itself.
VALIDATE_TIMEOUT = int(os.environ.get("AMEX_BROWSER_VALIDATE_TIMEOUT", "12"))

# Every name a Chromium-family browser is invoked by, in preference order:
# real Chromium first (what these scripts were written against), then Chrome,
# then the other forks, which work but are less tested here.
BROWSER_NAMES = (
    ["chromium.exe", "chrome.exe", "msedge.exe", "brave.exe"]
    if WINDOWS
    else [
        "chromium",              # Arch, Alpine, Fedora, Debian, snap shim
        "chromium-browser",      # Debian/Ubuntu deb, Alpine
        "chromium-freeworld",    # Fedora rpmfusion
        "google-chrome",
        "google-chrome-stable",
        "chrome",
        "brave-browser",
        "brave",
        "microsoft-edge",
        "microsoft-edge-stable",
        "vivaldi-stable",
        "vivaldi",
    ]
)

DRIVER_NAMES = ["chromedriver.exe"] if WINDOWS else ["chromedriver"]

_validated = {}
_validate_reason = {}


def _expand(paths):
    """Expand ~ and environment variables, dropping anything that does not
    resolve. The Windows entries reference %ProgramFiles% and friends, which
    are unset elsewhere and would otherwise expand to a literal."""
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
            "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
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
    # Linux, spread across the packaging schemes a user might plausibly have.
    # PATH already covers the common case; these are the locations a distro or
    # vendor uses that are deliberately not on PATH, or that PATH misses when
    # the tool runs under a service manager with a minimal environment.
    return _expand([
        # Ubuntu 24.04 ships chromium only as a snap, which is what broke the
        # original hardcoded /usr/bin path.
        "/snap/bin/chromium",
        "/snap/chromium/current/usr/lib/chromium-browser/chrome",
        # Debian, Ubuntu deb, Arch, Alpine, Fedora
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/lib/chromium/chromium",
        "/usr/lib/chromium/chrome",
        "/usr/lib/chromium-browser/chromium-browser",
        "/usr/lib64/chromium-browser/chromium-browser",
        "/usr/lib64/chromium-browser/chrome",
        # Vendor .deb / .rpm installs
        "/opt/google/chrome/chrome",
        "/opt/google/chrome/google-chrome",
        "/opt/google/chrome-beta/chrome",
        "/opt/chromium.org/chromium/chrome",
        "/opt/microsoft/msedge/msedge",
        "/opt/brave.com/brave/brave-browser",
        "/opt/brave.com/brave/brave",
        "/opt/vivaldi/vivaldi",
        # NixOS and Guix put everything under a store path, so the profile
        # symlink is the only stable name.
        "/run/current-system/sw/bin/chromium",
        "~/.nix-profile/bin/chromium",
        # Flatpak, system-wide and per-user
        "/var/lib/flatpak/exports/bin/org.chromium.Chromium",
        "/var/lib/flatpak/exports/bin/com.google.Chrome",
        "/var/lib/flatpak/exports/bin/com.brave.Browser",
        "~/.local/share/flatpak/exports/bin/org.chromium.Chromium",
        "~/.local/share/flatpak/exports/bin/com.google.Chrome",
    ])


def _windows_registry_browsers():
    """Windows records installed browsers under App Paths. Reading it beats
    guessing at Program Files, which misses per-user and relocated installs."""
    if not WINDOWS:
        return []
    found = []
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for exe in ("chrome.exe", "msedge.exe", "brave.exe", "chromium.exe"):
                try:
                    key = winreg.OpenKey(
                        hive,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\" + exe)
                    with key:
                        path = winreg.QueryValue(key, None)
                    if path:
                        found.append(path.strip('"'))
                except OSError:
                    continue
    except Exception:
        return []
    return found


def _resolve_wrapper(path):
    """Turn a snap or flatpak launcher into the real browser binary.

    /snap/bin/chromium is a symlink to /usr/bin/snap: a launcher, not the
    browser. Handing that to chromedriver as binary_location breaks it, since
    the driver launches the wrapper, the wrapper re-execs the real browser, and
    the process the driver is tracking exits. Measured on Ubuntu 24.04 with
    snap chromium 151: the wrapper failed against the chromedriver shipped in
    the same snap and happened to work against a downloaded one, which is a
    version-dependent accident rather than something to rely on. The real
    binary worked with both.

    Launching a wrapper from a shell is fine, so only binary_location callers
    are affected; resolving here keeps every caller on the path that works.
    """
    if not path or WINDOWS:
        return path
    real = os.path.realpath(path)
    is_snap = path.startswith("/snap/bin/") or real == "/usr/bin/snap"
    is_flatpak = "/flatpak/exports/bin/" in path
    if not (is_snap or is_flatpak):
        return path
    name = os.path.basename(path).split(".")[0]
    inner = ("usr/lib/chromium-browser/chrome", "usr/lib/chromium/chrome",
             "opt/google/chrome/chrome", "usr/bin/chromium")
    roots = [f"/snap/{name}/current"] if is_snap else [
        "/var/lib/flatpak/app/org.chromium.Chromium/current/active/files",
        os.path.expanduser(
            "~/.local/share/flatpak/app/org.chromium.Chromium/current/active/files"),
    ]
    for root in roots:
        for tail in inner:
            cand = os.path.join(root, tail)
            if os.path.exists(cand):
                return cand
    # No inner binary located. The wrapper is still worth returning: validation
    # decides whether it is usable, and for flatpak in particular the wrapper
    # may be the only entry point that exists.
    return path


def _playwright_chromium():
    """The browsers Playwright installed for this repo, newest first. Globbed
    rather than asked, because asking spawns a node process; the API is the
    last resort in candidates()."""
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not root:
        if MACOS:
            root = os.path.expanduser("~/Library/Caches/ms-playwright")
        elif WINDOWS:
            root = os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright")
        else:
            root = os.path.expanduser("~/.cache/ms-playwright")

    def revision(path):
        for part in path.split(os.sep):
            if part.startswith(("chromium-", "chromium_headless_shell-")):
                tail = part.split("-", 1)[1]
                return int(tail) if tail.isdigit() else -1
        return -1

    out = []
    # Playwright's layout moves between versions (chrome-linux became
    # chrome-linux64; the macOS bundle went from Chromium.app to "Google Chrome
    # for Testing.app"), so these glob the shapes rather than any one version's
    # names. Full builds rank above the headless shell, which cannot do the
    # headed handoff this repo needs.
    for base in ("chromium-*", "chromium_headless_shell-*"):
        hits = []
        for pat in (
            os.path.join(root, base, "chrome-linux*", "chrome"),
            os.path.join(root, base, "chrome-linux*", "headless_shell"),
            os.path.join(root, base, "chrome-mac*", "*.app", "Contents", "MacOS", "*"),
            os.path.join(root, base, "chrome-win*", "chrome.exe"),
        ):
            hits += [h for h in glob.glob(pat) if os.access(h, os.X_OK)]
        out += sorted(set(hits), key=revision, reverse=True)
    return out


def _playwright_api_chromium():
    """Ask Playwright itself, for layouts the globs do not know about. Run in a
    subprocess: tearing the sync API down mid-import spews "Task was destroyed
    but it is pending!" and a TargetClosedError onto stderr, which would land
    right after our own error message and read like a crash."""
    probe = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p: print(p.chromium.executable_path)\n"
    )
    try:
        out = subprocess.run([sys.executable, "-c", probe],
                             capture_output=True, text=True, timeout=60)
        path = out.stdout.strip()
        if path and os.path.exists(path):
            return [path]
    except Exception:
        pass
    return []


def _env_candidates():
    """Overrides, in precedence order. Each accepts a full path or a bare name,
    so AMEX_CHROMIUM=brave-browser works as well as a full path."""
    for var in ("AMEX_CHROMIUM", "CHROME_BIN", "CHROMIUM_BIN"):
        value = os.environ.get(var)
        if not value:
            continue
        if os.path.exists(value):
            yield value, var
            continue
        located = shutil.which(value)
        if located:
            yield located, f"{var} (name resolved on PATH)"
            continue
        # An override that resolves to nothing is a typo worth reporting. Only
        # AMEX_CHROMIUM is ours, so only that one is fatal; CHROME_BIN may
        # legitimately be stale in someone else's environment.
        if var == "AMEX_CHROMIUM":
            raise SystemExit(
                f"AMEX_CHROMIUM is set to {value!r}, which is neither a file "
                "nor a program on PATH. Fix it, or unset it to fall back to "
                "the normal search.")


def candidates():
    """Every plausible browser, in preference order, as (path, source) pairs.
    Deduplicated by resolved path so the same binary reached two ways is only
    tried once."""
    seen = set()

    def add(path, source):
        if not path:
            return None
        path = _resolve_wrapper(path)
        key = os.path.realpath(path)
        if key in seen or not os.path.exists(path):
            return None
        seen.add(key)
        return (path, source)

    out = []
    for path, source in _env_candidates():
        hit = add(path, source)
        if hit:
            out.append(hit)
    for name in BROWSER_NAMES:
        hit = add(shutil.which(name), "PATH")
        if hit:
            out.append(hit)
    # Mainly an escape hatch for testing the lower layers in isolation, since
    # these are absolute paths that an emptied PATH cannot hide.
    if not os.environ.get("AMEX_BROWSER_SKIP_DIRS"):
        for path in _browser_dirs() + _windows_registry_browsers():
            hit = add(path, "install location")
            if hit:
                out.append(hit)
    for path in _playwright_chromium():
        hit = add(path, "playwright")
        if hit:
            out.append(hit)
    return out


def _os_release():
    info = {}
    try:
        with open("/etc/os-release") as fh:
            for line in fh:
                if "=" in line:
                    key, _, value = line.partition("=")
                    info[key.strip()] = value.strip().strip('"')
    except OSError:
        pass
    return info


def _distro_install_hint():
    """The install command for THIS machine, not a menu of every distro's.

    A user staring at "install a browser" on Fedora does not need Ubuntu's
    apt line; giving them the wrong one is barely better than giving them
    nothing. Falls back to a generic list only when the distro is unknown.
    """
    if MACOS:
        return "brew install --cask chromium"
    if WINDOWS:
        return "winget install Google.Chrome"
    info = _os_release()
    family = f"{info.get('ID', '')} {info.get('ID_LIKE', '')}".lower()
    table = [
        (("ubuntu",), "sudo snap install chromium   (or: sudo apt install -y chromium-browser)"),
        (("debian",), "sudo apt install -y chromium"),
        (("fedora", "rhel", "centos"), "sudo dnf install -y chromium"),
        (("arch",), "sudo pacman -S --noconfirm chromium"),
        (("suse", "opensuse"), "sudo zypper install -y chromium"),
        (("alpine",), "sudo apk add chromium chromium-chromedriver"),
        (("nixos",), "nix-env -iA nixpkgs.chromium"),
        (("gentoo",), "sudo emerge www-client/chromium"),
        (("void",), "sudo xbps-install -y chromium"),
    ]
    for keys, command in table:
        if any(k in family for k in keys):
            return command
    return "install chromium with your distribution's package manager"


def _playwright_install_hint():
    """The no-root path, which works on every distro. This repo already
    depends on playwright, so its browser download is the most portable
    remedy available and the only one that needs no package manager."""
    return f"{os.path.basename(sys.executable)} -m playwright install chromium"


def _missing_library(text):
    """Pull the library name out of a loader failure.

    This is the single most common way a browser that is definitely installed
    still refuses to start: 'playwright install chromium' without
    'install-deps' leaves a browser that dies on libatk-1.0.so.0, and a
    minimal container image hits the same wall. The name is what makes the
    fix findable, so it gets surfaced verbatim.
    """
    for line in (text or "").splitlines():
        if "error while loading shared libraries" in line or "cannot open shared object file" in line:
            for token in line.replace(":", " ").split():
                if ".so" in token:
                    return token.strip(",")
            return line.strip()
    return None


def validate(path):
    """Return the browser's version string, or None if it cannot run.

    `--version` is cheap, needs no display, and rejects exactly the failures a
    path list cannot see: a wrapper whose target is gone, a partially
    downloaded browser, a package missing a shared library. It is not proof
    that chromedriver can drive it, which is why start_chrome also recovers.
    The reason for a failure is kept so the error message can name it.
    """
    if path in _validated:
        return _validated[path]
    result, reason = None, None
    try:
        proc = subprocess.run([path, "--version"], capture_output=True,
                              text=True, timeout=VALIDATE_TIMEOUT)
        blob = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0:
            lines = (proc.stdout or proc.stderr).strip().splitlines()
            if lines and any(ch.isdigit() for ch in lines[0]):
                result = lines[0].strip()
            else:
                reason = "ran but did not report a version"
        else:
            missing = _missing_library(blob)
            if missing:
                reason = f"missing system library {missing}"
            else:
                first = blob.strip().splitlines()
                reason = (first[0][:90] if first
                          else f"exited {proc.returncode}")
    except subprocess.TimeoutExpired:
        reason = f"did not respond within {VALIDATE_TIMEOUT}s"
    except PermissionError:
        reason = "not executable by this user"
    except OSError as exc:
        reason = str(exc)[:90]
    except ValueError as exc:
        reason = str(exc)[:90]
    _validated[path] = result
    _validate_reason[path] = reason
    return result


def _self_install_chromium():
    """Last-resort self-repair: have Playwright download a browser.

    This is the one remedy that works on every distribution and needs no root,
    no package manager, and no knowledge of how this machine packages things.
    The repo already depends on playwright and its documented setup runs this
    exact command, so doing it automatically is finishing the install rather
    than reaching beyond it. Set AMEX_BROWSER_NO_AUTO_INSTALL=1 to be told the
    command instead of having it run.
    """
    if os.environ.get("AMEX_BROWSER_NO_AUTO_INSTALL"):
        return None
    try:
        import playwright  # noqa: F401
    except ImportError:
        return None
    sys.stderr.write(
        "[browser_paths] No usable browser found. Downloading one with "
        "'playwright install chromium' (set AMEX_BROWSER_NO_AUTO_INSTALL=1 to "
        "skip this). This runs once.\n")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            timeout=900)
    except (subprocess.TimeoutExpired, OSError) as exc:
        sys.stderr.write(f"[browser_paths] automatic install failed: {exc}\n")
        return None
    if proc.returncode != 0:
        sys.stderr.write("[browser_paths] automatic install did not succeed.\n")
        return None
    _validated.clear()
    _validate_reason.clear()
    for path in _playwright_chromium():
        if validate(path):
            sys.stderr.write(f"[browser_paths] installed and verified: {path}\n")
            return path
    return None


def _no_browser_message(found):
    """Explain what was tried, what went wrong with each, and the specific
    command that fixes it on this machine. A hard failure that names no remedy
    just moves the work to the user."""
    lines = []
    if found:
        lines.append("A browser was found but none of them would run:")
        for path, source in found[:8]:
            reason = _validate_reason.get(path) or "failed to run"
            lines.append(f"  {path}")
            lines.append(f"    from {source}: {reason}")
        missing = [p for p in found
                   if "missing system library" in (_validate_reason.get(p[0]) or "")]
        if missing:
            lines.append("")
            lines.append(
                "A missing system library means the browser is installed but "
                "its dependencies are not. Install them with:")
            lines.append(
                f"  sudo {os.path.basename(sys.executable)} -m playwright "
                "install-deps chromium")
            lines.append(
                "  (that is the step most often skipped; 'playwright install "
                "chromium' alone downloads a browser that cannot start)")
    else:
        lines.append("No Chromium-family browser found on this machine.")
    lines.append("")
    lines.append("Fix it with any one of:")
    lines.append(f"  {_distro_install_hint()}")
    lines.append(f"  {_playwright_install_hint()}")
    lines.append("  set AMEX_CHROMIUM to the full path of a browser you have")
    lines.append("")
    lines.append("To see the whole search and each candidate's status:")
    lines.append(f"  {os.path.basename(sys.executable)} browser_paths.py --list")
    return "\n".join(lines)


def find_chromium(required=True, verify=True, self_install=True):
    """Return a browser path that has been shown to run.

    Set verify=False (or AMEX_BROWSER_NO_VALIDATE=1) to take the first
    candidate without running it, which is faster but trusts the guess.
    """
    if os.environ.get("AMEX_BROWSER_NO_VALIDATE"):
        verify = False
    found = candidates()
    if not verify:
        if found:
            return found[0][0]
    else:
        for path, _source in found:
            if validate(path):
                return path
        for path in _playwright_api_chromium():
            if validate(path):
                return path
    if self_install:
        repaired = _self_install_chromium()
        if repaired:
            return repaired
    if not required:
        return None
    raise SystemExit(_no_browser_message(found))


def _driver_dirs(browser=None):
    candidates_ = []
    # A snap browser ships a chromedriver built against exactly that build, so
    # it beats downloading one: no network, and no version skew.
    if browser and browser.startswith("/snap/"):
        candidates_.append("/snap/bin/chromium.chromedriver")
    if MACOS:
        candidates_ += ["/opt/homebrew/bin/chromedriver", "/usr/local/bin/chromedriver"]
    elif WINDOWS:
        candidates_ += [r"%LOCALAPPDATA%\chromedriver\chromedriver.exe"]
    else:
        candidates_ += [
            # /snap/bin/chromium.chromedriver is deliberately NOT here: it can
            # only drive the browser inside its own snap, so offering it to a
            # non-snap browser buys a guaranteed failed launch before the
            # fallback. The snap branch above adds it when it applies.
            "/usr/bin/chromedriver",
            "/usr/local/bin/chromedriver",
            "/usr/lib/chromium/chromedriver",
            "/usr/lib/chromium-browser/chromedriver",
        ]
    return _expand(candidates_)


def find_chromedriver(browser=None):
    """Return a chromedriver path, or None meaning 'let Selenium Manager fetch
    one'. None is a normal result, not an error: a downloaded driver matched to
    the detected browser beats any path guess, and selenium >= 4.6 does that
    automatically."""
    override = os.environ.get("AMEX_CHROMEDRIVER")
    if override:
        if os.path.exists(override):
            return override
        located = shutil.which(override)
        if located:
            return located
        raise SystemExit(
            f"AMEX_CHROMEDRIVER is set to {override!r}, which is neither a "
            "file nor a program on PATH. Fix it, or unset it to let selenium "
            "download a matching driver.")
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
    when none is installed. Selenium is imported lazily so this module stays
    importable, and runnable as a CLI, without it."""
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


def start_chrome(options, browser=None):
    """Start a selenium Chrome session, recovering from a driver that cannot
    drive this browser.

    Driver/browser mismatch is a normal condition, not an exceptional one: a
    distro chromedriver goes stale against an auto-updating browser, and a
    snap's driver refuses a browser outside its snap. The preferred driver is
    tried first because it is local and version-matched, and Selenium Manager
    is the fallback because it downloads one built for the browser actually
    found. Failing over costs one wasted launch; not failing over costs the
    whole run.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service

    if browser is None:
        browser = getattr(options, "binary_location", "") or find_chromium()
    preferred = find_chromedriver(browser)
    if preferred:
        try:
            return webdriver.Chrome(service=Service(preferred), options=options)
        except Exception as exc:
            sys.stderr.write(
                f"[browser_paths] chromedriver at {preferred} could not start "
                f"the browser ({type(exc).__name__}); retrying with a driver "
                "downloaded to match it.\n")
    return webdriver.Chrome(service=build_chrome_service_manager(), options=options)


def build_chrome_service_manager():
    """A Service with no path, which makes selenium resolve the driver through
    Selenium Manager."""
    from selenium.webdriver.chrome.service import Service
    return Service()


def _cmd_list():
    found = candidates()
    if not found:
        print("No candidates found.")
        return 1
    print(f"{len(found)} candidate(s), in preference order:")
    for i, (path, source) in enumerate(found, 1):
        version = validate(path)
        status = version if version else "WILL NOT RUN"
        print(f"  {i}. {path}")
        print(f"     source: {source}   status: {status}")
    chosen = find_chromium(required=False)
    print(f"\nchosen browser: {chosen or '(none usable)'}")
    print(f"chosen driver : {find_chromedriver(chosen) or 'None (Selenium Manager)'}")
    return 0


def _cmd_selftest():
    browser = find_chromium()
    print("browser:", browser)
    print("version:", validate(browser))
    print("driver :", find_chromedriver(browser) or "None (Selenium Manager)")
    try:
        from selenium import webdriver
    except ImportError:
        print("selenium not installed; skipping the launch test.")
        print("SELFTEST PASS (discovery only)")
        return 0
    options = webdriver.ChromeOptions()
    options.binary_location = browser
    for arg in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage"):
        options.add_argument(arg)
    driver = start_chrome(options, browser)
    try:
        driver.get("about:blank")
        print("launched:", driver.capabilities.get("browserVersion"))
    finally:
        driver.quit()
    print("SELFTEST PASS")
    return 0


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else "--chromium"
    if what in ("--chromium", "--browser"):
        print(find_chromium())
    elif what == "--chromedriver":
        path = find_chromedriver(find_chromium(required=False))
        if not path:
            raise SystemExit(
                "No chromedriver on this machine. Selenium >= 4.6 downloads "
                "one on demand, so the Python scripts are fine; this only "
                "matters if you needed the path itself.")
        print(path)
    elif what == "--list":
        return _cmd_list()
    elif what == "--selftest":
        return _cmd_selftest()
    else:
        raise SystemExit(
            f"usage: {sys.argv[0]} [--chromium|--chromedriver|--list|--selftest]")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
