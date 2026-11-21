"""
Instagram -> Telegram backup bot.

- Fetches public Posts (photo / video / carousel) and Reels via Instaloader
  WITHOUT logging in with Instagram credentials.
- Sends media to a Telegram channel via the Bot API.
- Tracks already-sent posts in state.json (per-account latest shortcode + UTC timestamp).
- Runs nicely on GitHub Actions (see .github/workflows/main.yml).

Required environment variables:
    TELEGRAM_BOT_TOKEN  - token from @BotFather
    TELEGRAM_CHAT_ID    - channel id, e.g. -1001234567890

Files:
    accounts.txt - one Instagram username per line ('#' comments and blank lines ignored)
    state.json   - {"username": {"shortcode": "...", "timestamp": "2024-01-01T00:00:00Z"}}
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    import instaloader
    from instaloader import Instaloader, Post, Profile
    from instaloader.exceptions import (
        ConnectionException,
        LoginRequiredException,
        PrivateProfileNotFollowedException,
        ProfileNotExistsException,
        QueryReturnedBadRequestException,
        TooManyRequestsException,
    )
except ImportError:  # pragma: no cover - handled with a clear message at runtime
    instaloader = None  # type: ignore[assignment]
    Instaloader = None  # type: ignore[assignment,misc]
    Post = None  # type: ignore[assignment,misc]
    Profile = None  # type: ignore[assignment,misc]
    ConnectionException = Exception  # type: ignore[assignment,misc]
    LoginRequiredException = Exception  # type: ignore[assignment,misc]
    PrivateProfileNotFollowedException = Exception  # type: ignore[assignment,misc]
    ProfileNotExistsException = Exception  # type: ignore[assignment,misc]
    QueryReturnedBadRequestException = Exception  # type: ignore[assignment,misc]
    TooManyRequestsException = Exception  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
ACCOUNTS_FILE = BASE_DIR / "accounts.txt"
STATE_FILE = BASE_DIR / "state.json"
DOWNLOADS_DIR = BASE_DIR / "downloads"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_API_BASE = "https://api.telegram.org"

# How many of the newest posts to look at per account on subsequent runs.
# We stop as soon as we hit a post that is not newer than the saved state,
# so this is just a safety cap to avoid scanning a whole profile.
MAX_POSTS_TO_CHECK = int(os.environ.get("MAX_POSTS_TO_CHECK", "30"))

# Instaloader never yields Stories here. profile.get_posts() yields the post
# feed (photo / video / sidecar) and profile.get_reels() (when available in
# the installed Instaloader version) yields Reels; both are merged and
# de-duplicated by shortcode below, so Reels are covered in either case.

# Politeness / rate-limit delays (seconds)
DELAY_BETWEEN_ACCOUNTS = int(os.environ.get("DELAY_BETWEEN_ACCOUNTS", "15"))
DELAY_BETWEEN_POSTS = int(os.environ.get("DELAY_BETWEEN_POSTS", "5"))
DELAY_AFTER_RATE_LIMIT = int(os.environ.get("DELAY_AFTER_RATE_LIMIT", "120"))

# Telegram limits
TELEGRAM_CAPTION_LIMIT = 1024          # caption limit for media
TELEGRAM_MESSAGE_LIMIT = 4096          # text message limit
TELEGRAM_MEDIA_GROUP_MAX = 10          # sendMediaGroup accepts 2-10 items

SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
SUPPORTED_VIDEO_EXTS = {".mp4", ".mov", ".webm"}
DOWNLOAD_EXTS = SUPPORTED_IMAGE_EXTS | SUPPORTED_VIDEO_EXTS


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    """Print a timestamped log line (visible in the GitHub Actions log)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {message}", flush=True)


def utc_iso(dt: datetime) -> str:
    """Format a datetime as ISO-8601 in UTC, e.g. 2024-05-01T12:34:56Z."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def make_hashtag(username: str) -> str:
    """Telegram hashtags cannot contain '.', so 'user.name' -> '#user_name'."""
    return "#" + username.replace(".", "_").strip().lstrip("@")


def build_caption(username: str, post: Any) -> str:
    """
    Caption = hashtag on the first line, then the original post caption.

    Telegram media captions are limited to 1024 characters, so the original
    caption is truncated (with an ellipsis) when needed.
    """
    hashtag = make_hashtag(username)
    original = (getattr(post, "caption", None) or "").strip()

    # Also add a link back to the original post - handy for searching / credit.
    shortcode = getattr(post, "shortcode", "")
    source_link = f"https://www.instagram.com/p/{shortcode}/" if shortcode else ""

    prefix = hashtag
    if source_link:
        prefix = f"{hashtag}\n{source_link}"

    if not original:
        return prefix[:TELEGRAM_CAPTION_LIMIT]

    max_original_len = TELEGRAM_CAPTION_LIMIT - len(prefix) - 2  # "\n\n" separator
    if max_original_len <= 0:
        return prefix[:TELEGRAM_CAPTION_LIMIT]

    if len(original) > max_original_len:
        # Keep room for the ellipsis.
        original = original[: max_original_len - 1].rstrip() + "…"

    return f"{prefix}\n\n{original}"


# ---------------------------------------------------------------------------
# accounts.txt / state.json
# ---------------------------------------------------------------------------

def load_accounts(path: Path = ACCOUNTS_FILE) -> list[str]:
    if not path.exists():
        log(f"⚠️  accounts.txt not found at {path} - nothing to do.")
        return []

    usernames: list[str] = []
    seen: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        username = line.lstrip("@").strip()
        lowered = username.lower()
        if username and lowered not in seen:
            seen.add(lowered)
            usernames.append(username)
    return usernames


def load_state(path: Path = STATE_FILE) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log(f"⚠️  Could not parse state.json ({exc}); starting from empty state.")
        return {}
    if not isinstance(data, dict):
        log("⚠️  state.json is not a JSON object; starting from empty state.")
        return {}
    return data


def save_state(state: dict[str, dict[str, Any]], path: Path = STATE_FILE) -> None:
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)
    log(f"💾 state.json updated ({len(state)} account(s) tracked).")


# ---------------------------------------------------------------------------
# Telegram Bot API
# ---------------------------------------------------------------------------

class TelegramError(RuntimeError):
    pass


def telegram_api_url(method: str) -> str:
    return f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/{method}"


def _check_telegram_response(response: requests.Response, method: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise TelegramError(
            f"Telegram {method}: non-JSON response (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        ) from exc

    if not response.ok or not payload.get("ok", False):
        description = payload.get("description", "unknown error")
        # Respect Telegram's rate limiting hint if present.
        retry_after = (
            payload.get("parameters", {}).get("retry_after")
            if isinstance(payload.get("parameters"), dict)
            else None
        )
        if retry_after:
            log(f"⏳ Telegram rate limit on {method}; sleeping {retry_after}s…")
            time.sleep(int(retry_after) + 1)
        raise TelegramError(
            f"Telegram {method} failed (HTTP {response.status_code}): {description}"
        )
    return payload


def send_text_message(text: str) -> None:
    """Fallback for posts with no downloadable media (rare)."""
    response = requests.post(
        telegram_api_url("sendMessage"),
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[:TELEGRAM_MESSAGE_LIMIT],
            "disable_web_page_preview": "false",
        },
        timeout=60,
    )
    _check_telegram_response(response, "sendMessage")


def send_single_media(file_path: Path, caption: str, is_video: bool) -> None:
    method = "sendVideo" if is_video else "sendPhoto"
    field = "video" if is_video else "photo"
    with file_path.open("rb") as fh:
        files = {field: (file_path.name, fh)}
        data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
        if is_video:
            data["supports_streaming"] = "true"
        response = requests.post(
            telegram_api_url(method), data=data, files=files, timeout=300
        )
    _check_telegram_response(response, method)


def send_media_group(file_paths: list[Path], caption: str) -> None:
    """
    Send 2-10 media items as one album via sendMediaGroup.

    The caption is attached to the first item only (Telegram shows it once).
    """
    if not (2 <= len(file_paths) <= TELEGRAM_MEDIA_GROUP_MAX):
        raise ValueError(
            f"sendMediaGroup needs 2-{TELEGRAM_MEDIA_GROUP_MAX} items, got {len(file_paths)}"
        )

    media: list[dict[str, Any]] = []
    files: dict[str, tuple[str, Any]] = {}

    opened: list[Any] = []
    try:
        for index, file_path in enumerate(file_paths):
            attach_name = f"file{index}"
            is_video = file_path.suffix.lower() in SUPPORTED_VIDEO_EXTS
            item: dict[str, Any] = {
                "type": "video" if is_video else "photo",
                "media": f"attach://{attach_name}",
            }
            if is_video:
                item["supports_streaming"] = True
            if index == 0:
                item["caption"] = caption
            media.append(item)

            fh = file_path.open("rb")
            opened.append(fh)
            files[attach_name] = (file_path.name, fh)

        response = requests.post(
            telegram_api_url("sendMediaGroup"),
            data={"chat_id": TELEGRAM_CHAT_ID, "media": json.dumps(media)},
            files=files,
            timeout=600,
        )
        _check_telegram_response(response, "sendMediaGroup")
    finally:
        for fh in opened:
            try:
                fh.close()
            except Exception:
                pass


def send_media_files(file_paths: list[Path], caption: str) -> None:
    """
    Send downloaded files for one Instagram post.

    - 0 files -> plain text message (caption + link)
    - 1 file  -> sendPhoto / sendVideo
    - 2-10    -> one sendMediaGroup album
    - >10     -> chunked albums (caption only on the first chunk)
    """
    if not file_paths:
        send_text_message(caption)
        return

    if len(file_paths) == 1:
        only = file_paths[0]
        send_single_media(only, caption, only.suffix.lower() in SUPPORTED_VIDEO_EXTS)
        return

    for start in range(0, len(file_paths), TELEGRAM_MEDIA_GROUP_MAX):
        chunk = file_paths[start : start + TELEGRAM_MEDIA_GROUP_MAX]
        chunk_caption = caption if start == 0 else ""
        if len(chunk) == 1:
            only = chunk[0]
            send_single_media(only, chunk_caption, only.suffix.lower() in SUPPORTED_VIDEO_EXTS)
        else:
            # sendMediaGroup captions: only meaningful on first item; for later
            # chunks we pass an empty caption via the first item of that chunk.
            if start == 0:
                send_media_group(chunk, chunk_caption)
            else:
                send_media_group(chunk, "")
        if start + TELEGRAM_MEDIA_GROUP_MAX < len(file_paths):
            time.sleep(2)


# ---------------------------------------------------------------------------
# Instaloader: download one post, then send + cleanup
# ---------------------------------------------------------------------------

def collect_downloaded_files(target_dir: Path, before: set[Path]) -> list[Path]:
    """Return media files created in target_dir since the `before` snapshot."""
    after = {p for p in target_dir.rglob("*") if p.is_file()}
    new_files = [
        p
        for p in (after - before)
        if p.suffix.lower() in DOWNLOAD_EXTS and p.stat().st_size > 0
    ]
    # Instaloader names files like 2024-01-01_12-00-00_UTC.jpg / ..._1.jpg etc.
    # Sorting by name keeps carousel order (cover first).
    return sorted(new_files, key=lambda p: p.name)


def cleanup_files(paths: list[Path], target_dir: Path) -> None:
    """Delete downloaded media (and Instaloader sidecar .txt/.json.xz files)."""
    for file_path in paths:
        try:
            file_path.unlink(missing_ok=True)
        except OSError as exc:
            log(f"⚠️  Could not delete {file_path}: {exc}")

    # Remove any leftover metadata files Instaloader wrote for this post.
    if target_dir.exists():
        for leftover in target_dir.rglob("*"):
            if leftover.is_file():
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass
        # Remove empty directories (deepest first).
        for dirpath in sorted(
            (p for p in target_dir.rglob("*") if p.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        ):
            try:
                dirpath.rmdir()
            except OSError:
                pass
        try:
            target_dir.rmdir()
        except OSError:
            pass


def download_and_send_post(
    loader: "Instaloader", post: "Post", username: str
) -> bool:
    """
    Download one post into downloads/<username>/<shortcode>/, send it to
    Telegram, then delete the local files. Returns True on success.
    """
    shortcode = post.shortcode
    caption = build_caption(username, post)
    target_dir = DOWNLOADS_DIR / username / shortcode
    target_dir.mkdir(parents=True, exist_ok=True)

    before = {p for p in target_dir.rglob("*") if p.is_file()}
    media_files: list[Path] = []

    try:
        # download_post handles single photo, single video and sidecars,
        # and also downloads the Reel video file for video posts.
        loader.download_post(post, target=str(target_dir))
        media_files = collect_downloaded_files(target_dir, before)

        if not media_files:
            log(f"⚠️  No media files found for {username}/{shortcode}; sending text only.")
        else:
            kinds = ", ".join(
                "video" if p.suffix.lower() in SUPPORTED_VIDEO_EXTS else "photo"
                for p in media_files
            )
            log(f"📤 Sending {len(media_files)} file(s) [{kinds}] for @{username}/{shortcode}…")

        send_media_files(media_files, caption)
        log(f"✅ Sent @{username}/{shortcode} to Telegram.")
        return True

    except TelegramError as exc:
        log(f"❌ Telegram error for @{username}/{shortcode}: {exc}")
        return False
    except Exception as exc:  # Instaloader / network / filesystem errors
        log(f"❌ Failed to download/send @{username}/{shortcode}: {type(exc).__name__}: {exc}")
        return False
    finally:
        # Storage cleanup: always delete local media, success or failure,
        # so nothing gets committed back to the repository.
        cleanup_files(media_files, target_dir)


# ---------------------------------------------------------------------------
# Per-account logic (first-run rule + incremental updates)
# ---------------------------------------------------------------------------

def create_loader() -> "Instaloader":
    if Instaloader is None:
        raise RuntimeError(
            "instaloader is not installed. Run: pip install -r requirements.txt"
        )
    loader = Instaloader(
        # --- download only the media itself (smaller / faster) ---
        download_pictures=True,
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        # --- request behaviour ---
        max_connection_attempts=3,
        request_timeout=60.0,
        rate_controller=None,  # we add our own sleeps between accounts/posts
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    return loader


def get_profile(loader: "Instaloader", username: str) -> "Profile":
    # Public fetch only - no loader.login() anywhere in this project.
    return Profile.from_username(loader.context, username)


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def collect_new_posts(
    profile: "Profile",
    username: str,
    is_first_run: bool,
    last_seen_time: datetime | None,
    last_seen_shortcode: str | None,
) -> list:
    """
    Merge the post feed and (if available) the Reels feed.

    First run: return only the single most recent item overall.
    Later runs: return every item newer than the saved state (unsorted here;
    the caller sorts oldest-first before sending).
    """
    sources: list[tuple[str, Any]] = []
    try:
        sources.append(("posts", profile.get_posts()))
    except Exception as exc:
        log(f"⚠️  @{username}: could not open post feed: {type(exc).__name__}: {exc}")
    if hasattr(profile, "get_reels"):
        try:
            sources.append(("reels", profile.get_reels()))
        except Exception as exc:
            log(f"⚠️  @{username}: could not open reels feed: {type(exc).__name__}: {exc}")

    if is_first_run:
        latest = None
        for source_name, iterator in sources:
            try:
                candidate = next(iter(iterator))
            except StopIteration:
                continue
            except Exception as exc:
                log(f"⚠️  @{username}: {source_name} feed failed: "
                    f"{type(exc).__name__}: {exc}. Trying other feeds.")
                continue
            if latest is None or _as_utc(candidate.date_utc) > _as_utc(latest.date_utc):
                latest = candidate
        return [latest] if latest is not None else []

    collected: dict[str, Any] = {}
    for source_name, iterator in sources:
        try:
            for index, post in enumerate(iterator):
                if index >= MAX_POSTS_TO_CHECK:
                    log(f"ℹ️  @{username}: reached check limit "
                        f"({MAX_POSTS_TO_CHECK}) in {source_name} feed. Stopping that feed.")
                    break
                if last_seen_shortcode and post.shortcode == last_seen_shortcode:
                    break
                if last_seen_time and _as_utc(post.date_utc) <= last_seen_time:
                    break
                collected[post.shortcode] = post
        except (TooManyRequestsException, QueryReturnedBadRequestException) as exc:
            log(f"🚦 @{username}: rate limited in {source_name} feed ({exc}). "
                f"Sleeping {DELAY_AFTER_RATE_LIMIT}s, then continuing with other feeds.")
            time.sleep(DELAY_AFTER_RATE_LIMIT)
        except LoginRequiredException:
            log(f"🔒 @{username}: login required in {source_name} feed. Skipping that feed.")
        except ConnectionException as exc:
            log(f"🌐 @{username}: connection error in {source_name} feed ({exc}). Skipping that feed.")
        except Exception as exc:
            log(f"❌ @{username}: error in {source_name} feed: {type(exc).__name__}: {exc}. "
                "Skipping that feed.")
    return list(collected.values())


def process_account(
    loader: "Instaloader",
    username: str,
    state: dict[str, dict[str, Any]],
) -> bool:
    """
    Process one account. Returns True if state may have changed.

    First run (username not in state): send ONLY the single most recent post.
    Later runs: send every post newer than the saved timestamp, oldest first.
    """
    log(f"\n{'=' * 60}\n👤 Account: @{username}")

    try:
        profile = get_profile(loader, username)
    except ProfileNotExistsException:
        log(f"❌ @{username}: profile does not exist. Skipping.")
        return False
    except PrivateProfileNotFollowedException:
        log(f"🔒 @{username}: profile is private. Public fetch is impossible. Skipping.")
        return False
    except LoginRequiredException:
        log(
            f"🔒 @{username}: Instagram requires login for this profile right now "
            "(rate limit / checkpoint). Skipping this run - it may work next time."
        )
        return False
    except (TooManyRequestsException, QueryReturnedBadRequestException) as exc:
        log(f"🚦 @{username}: Instagram rate limited us ({exc}). "
            f"Sleeping {DELAY_AFTER_RATE_LIMIT}s, then skipping this account.")
        time.sleep(DELAY_AFTER_RATE_LIMIT)
        return False
    except ConnectionException as exc:
        log(f"🌐 @{username}: connection error ({exc}). Skipping.")
        return False
    except Exception as exc:
        log(f"❌ @{username}: unexpected error while loading profile: "
            f"{type(exc).__name__}: {exc}. Skipping.")
        return False

    if getattr(profile, "is_private", False):
        log(f"🔒 @{username}: profile is private. Skipping (public accounts only).")
        return False

    existing = state.get(username)
    is_first_run = existing is None
    last_seen_time = parse_utc_iso(existing.get("timestamp")) if existing else None
    last_seen_shortcode = existing.get("shortcode") if existing else None

    if is_first_run:
        log(f"🆕 @{username}: new account - only the single most recent post will be sent.")
    else:
        log(f"📌 @{username}: last seen post {last_seen_shortcode} at "
            f"{existing.get('timestamp')} - checking for newer posts…")

    # Collect candidate posts (Posts + Reels, never Stories).
    new_posts = collect_new_posts(
        profile, username, is_first_run, last_seen_time, last_seen_shortcode
    )

    if not new_posts:
        log(f"✔️  @{username}: no new posts.")
        # Still record first-run accounts with zero posts? No - without a post
        # there is no timestamp to save; it will be treated as new next run,
        # which is the correct behaviour.
        return False

    # Send in chronological order: oldest of the new posts first.
    new_posts.sort(key=lambda p: p.date_utc)
    log(f"📰 @{username}: {len(new_posts)} post(s) to send "
        f"(oldest first).")

    state_changed = False
    for position, post in enumerate(new_posts, start=1):
        post_time = post.date_utc
        if post_time.tzinfo is None:
            post_time = post_time.replace(tzinfo=timezone.utc)

        log(f"➡️  [{position}/{len(new_posts)}] @{username}/{post.shortcode} "
            f"({utc_iso(post_time)})")

        success = download_and_send_post(loader, post, username)
        if success:
            # Advance state only after a successful send, so a failed post
            # is retried on the next run instead of being lost forever.
            state[username] = {
                "shortcode": post.shortcode,
                "timestamp": utc_iso(post_time),
            }
            state_changed = True
            # Persist after every post: if the job is killed mid-run,
            # already-sent posts are not sent again.
            save_state(state)
        else:
            log(f"⚠️  @{username}: stopping here - @{username}/{post.shortcode} "
                "will be retried next run (state not advanced past it).")
            break

        if position < len(new_posts):
            time.sleep(DELAY_BETWEEN_POSTS)

    return state_changed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def validate_config() -> bool:
    ok = True
    if not TELEGRAM_BOT_TOKEN:
        log("❌ TELEGRAM_BOT_TOKEN is not set (GitHub Secret missing).")
        ok = False
    if not TELEGRAM_CHAT_ID:
        log("❌ TELEGRAM_CHAT_ID is not set (GitHub Secret missing).")
        ok = False
    if Instaloader is None:
        log("❌ instaloader is not installed. Run: pip install -r requirements.txt")
        ok = False
    return ok


def main() -> int:
    log("🚀 Instagram -> Telegram backup starting…")

    if not validate_config():
        log("🛑 Missing configuration - aborting.")
        return 1

    accounts = load_accounts()
    if not accounts:
        log("ℹ️  accounts.txt is empty - nothing to do.")
        return 0

    log(f"📋 {len(accounts)} account(s) to check: {', '.join('@' + a for a in accounts)}")

    state = load_state()
    loader = create_loader()
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    any_state_changed = False
    failures = 0

    for idx, username in enumerate(accounts):
        try:
            changed = process_account(loader, username, state)
            any_state_changed = any_state_changed or changed
        except Exception as exc:
            # Never let one account crash the whole workflow.
            failures += 1
            log(f"❌ @{username}: unhandled error: {type(exc).__name__}: {exc}. Continuing…")

        if idx < len(accounts) - 1:
            log(f"⏳ Waiting {DELAY_BETWEEN_ACCOUNTS}s before the next account…")
            time.sleep(DELAY_BETWEEN_ACCOUNTS)

    if any_state_changed:
        save_state(state)
    else:
        log("💾 state.json unchanged.")

    # Final cleanup: make sure the downloads folder holds no media at all.
    if DOWNLOADS_DIR.exists():
        for leftover in sorted(DOWNLOADS_DIR.rglob("*"), reverse=True):
            try:
                if leftover.is_file():
                    leftover.unlink(missing_ok=True)
                elif leftover.is_dir():
                    leftover.rmdir()
            except OSError:
                pass

    log(f"🏁 Done. Accounts checked: {len(accounts)}, account-level failures: {failures}.")
    # Exit 0 even if some accounts failed (rate limits etc.) so the workflow
    # still commits the updated state.json for the accounts that succeeded.
    return 0


if __name__ == "__main__":
    sys.exit(main())
