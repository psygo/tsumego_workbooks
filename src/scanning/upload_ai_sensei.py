#!/usr/bin/env python3
"""Upload the played Sugeundaesajeon SGFs through AI Sensei's web form.

Setup in a virtual environment: python -m pip install playwright
For bundled Chromium: python -m playwright install chromium

Try one game first:
  python3 upload_ai_sensei.py sugeundaesajeon-2479-2634-played.zip --limit 1

Run the same command without --limit to upload the remaining games.
Use --dry-run to list the pending files without opening a browser.
For a different AI Sensei account, choose a separate --state-dir.

On macOS, to sign in manually before running with --chrome, open:
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    --user-data-dir="$HOME/.ai-sensei-sugeundaesajeon/chrome-browser" \
    https://ai-sensei.com/upload
Sign in to AI Sensei, quit that Chrome instance, then run with --chrome.
If using --state-dir, point the Chrome command at its chrome-browser subfolder.
"""

import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import sys
from time import monotonic
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile


UPLOAD_URL = "https://ai-sensei.com/upload"
GAME_URL = re.compile(r"https://ai-sensei\.com/game/[^/?#]+/[^/?#]+(?:[?#].*)?$")
SGF_INPUT = 'input[type="file"][accept*=".sgf"]'
DEFAULT_ZIP = "sugeundaesajeon-2479-2634-played.zip"
DEFAULT_STATE = Path.home() / ".ai-sensei-sugeundaesajeon"


def read_games(zip_path):
    expected = {f"problem-{number}.sgf" for number in range(2479, 2635)}
    games = {}
    with ZipFile(zip_path) as archive:
        for member in archive.infolist():
            name = PurePosixPath(member.filename).name
            if name not in expected or member.is_dir():
                continue
            if name in games:
                raise ValueError(f"The ZIP contains more than one copy of {name}.")
            data = archive.read(member)
            if not data.lstrip().startswith(b"(;") or re.search(rb"\nA[BW]\[", data):
                raise ValueError(f"{name} does not look like a converted, played-move SGF.")
            games[name] = data
    missing = sorted(expected - games.keys())
    if missing:
        raise ValueError(f"The ZIP is missing {len(missing)} SGFs, starting with {missing[0]}.")
    return sorted(games.items())


def record_progress(path, progress, key, url):
    progress[key] = url
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(progress, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def upload_one(page, name, data, timeout):
    response_errors = []

    def track_limit(response):
        url = urlsplit(response.url)
        if (url.hostname == "ai-sensei.com"
                and "/user/game-uploads/" in url.path and response.status == 429):
            retry_after = response.headers.get("retry-after")
            message = "AI Sensei rate-limited the upload (HTTP 429)."
            if retry_after:
                message += f" Retry-After: {retry_after}."
            response_errors.append(message)

    page.on("response", track_limit)
    try:
        page.locator(SGF_INPUT).set_input_files({
            "name": name,
            "mimeType": "application/x-go-sgf",
            "buffer": data,
        })
        upload = page.get_by_role("button", name="Upload", exact=True).filter(visible=True)
        duplicate = page.get_by_text("Game Already Analyzed", exact=True).filter(visible=True)
        existing = page.get_by_role(
            "button", name="Go to game", exact=True
        ).or_(page.get_by_role("link", name="Go to game", exact=True)).filter(visible=True)
        error = page.locator(".alert-danger:visible")
        submitted = opened_existing = False
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            if response_errors:
                raise RuntimeError(response_errors[0])
            if GAME_URL.fullmatch(page.url):
                return page.url, opened_existing
            if duplicate.count() and not opened_existing and existing.count() == 1:
                existing.click(timeout=5000)
                opened_existing = True
            elif not submitted and not duplicate.count() and upload.count() == 1 and upload.is_enabled():
                upload.click(timeout=5000)
                submitted = True
            elif error.count():
                raise RuntimeError(error.first.inner_text(timeout=2000))
            page.wait_for_timeout(250)
        action = "confirm the review link" if submitted or opened_existing else "find the Upload button"
        raise RuntimeError(f"Timed out waiting to {action}. Check the browser and My Games before rerunning.")
    finally:
        page.remove_listener("response", track_limit)


def run_browser(args, pending, progress_path, progress):
    from playwright.sync_api import Error, TimeoutError, sync_playwright

    args.state_dir.mkdir(parents=True, exist_ok=True)
    browser_dir = args.state_dir / ("chrome-browser" if args.chrome else "browser")
    launch_options = {}
    if args.chrome:
        launch_options["channel"] = "chrome"
        if sys.platform == "darwin":
            # Reuse Chrome's normal Keychain access for the manually signed-in profile.
            launch_options["ignore_default_args"] = ["--use-mock-keychain", "--password-store=basic"]
    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(browser_dir), headless=False, locale="en-US", **launch_options,
            )
        except Error as exc:
            browser = "Google Chrome" if args.chrome else "Chromium"
            raise RuntimeError(f"Could not open {browser}: {exc}") from exc
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(15000)
            if args.chrome:
                print(f"Opening AI Sensei with the Chrome profile at {browser_dir}.", flush=True)
            else:
                print("Sign in to AI Sensei in the browser if prompted. The script will continue automatically.",
                      flush=True)
            page.goto(UPLOAD_URL, wait_until="domcontentloaded")
            try:
                page.locator(SGF_INPUT).wait_for(state="attached", timeout=300000)
            except TimeoutError as exc:
                message = "AI Sensei's SGF file picker did not appear after five minutes."
                if args.chrome:
                    message += " Check that AI Sensei is signed in when this profile is opened manually."
                raise RuntimeError(message) from exc

            for index, (name, data, key) in enumerate(pending, 1):
                print(f"[{index}/{len(pending)}] {name}", flush=True)
                try:
                    url, already_analyzed = upload_one(page, name, data, args.timeout)
                    record_progress(progress_path, progress, key, url)
                except (Error, RuntimeError, OSError) as exc:
                    raise RuntimeError(f"Stopped on {name}: {exc}") from exc
                status = "Already analyzed" if already_analyzed else "Uploaded"
                print(f"  {status}: {url}", flush=True)
                if index < len(pending):
                    page.wait_for_timeout(int(args.delay * 1000))
                    page.goto(UPLOAD_URL, wait_until="domcontentloaded")
        except Error as exc:
            raise RuntimeError(f"Browser stopped: {exc}") from exc
        finally:
            context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=Path, nargs="?", default=Path(DEFAULT_ZIP))
    parser.add_argument("--dry-run", action="store_true", help="List pending SGFs and exit")
    parser.add_argument("--chrome", action="store_true",
                        help="Use installed Google Chrome with a separate, persistent profile")
    parser.add_argument("--limit", type=int, help="Process at most this many pending SGFs")
    parser.add_argument("--delay", type=int, default=5, help="Seconds between uploads (default: 5)")
    parser.add_argument("--timeout", type=int, default=60, help="Seconds to confirm a game link (default: 60)")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE,
                        help="Folder for the browser profile and upload log")
    args = parser.parse_args()
    if (args.limit is not None and args.limit < 1) or args.delay < 1 or args.timeout < 1:
        parser.error("--limit, --delay and --timeout must be positive")

    try:
        games = read_games(args.archive)
        progress_path = args.state_dir / "uploaded.json"
        progress = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {}
        if not isinstance(progress, dict):
            raise ValueError(f"Invalid progress file: {progress_path}")
        pending = []
        for name, data in games:
            key = f"{name}:{sha256(data).hexdigest()}"
            if key not in progress:
                pending.append((name, data, key))
        print(f"{len(games)} SGFs found. {len(games) - len(pending)} already recorded. "
              f"{len(pending)} pending.", flush=True)
        pending = pending[:args.limit]
        if args.dry_run:
            for name, _, _ in pending:
                print(name)
        elif pending:
            run_browser(args, pending, progress_path, progress)
            print(f"Review links saved to {progress_path}.")
        return 0
    except ModuleNotFoundError:
        print("Install Playwright with: python3 -m pip install playwright", file=sys.stderr)
        return 1
    except (BadZipFile, OSError, ValueError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Confirmed review links are saved.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
