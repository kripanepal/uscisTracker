"""
USCIS Case Watcher
Logs into USCIS, fetches case details via API, and tracks changes over time.
Supports multiple accounts and cases.
"""

import argparse
import base64
import copy
import difflib
import json
import logging
import os
import random
import signal
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from typing import Optional
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from selenium import webdriver
from pyotp import TOTP
from selenium.common.exceptions import SessionNotCreatedException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from deepdiff import DeepDiff
from summary import print_summary

# File paths
SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
OUTPUT_DIR = SCRIPT_DIR / "output"
SESSION_DIR = OUTPUT_DIR / "_sessions"
LOG_DIR = OUTPUT_DIR / "logs"
LOG_FILE = LOG_DIR / "watcher.log"

# Cookies older than this are not even attempted (USCIS sessions don't last forever)
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60  # 8 hours


def load_config() -> dict:
    """Load configuration from config.json"""
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Config file not found: {CONFIG_FILE}")

    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def get_schedule_settings(config: dict, args) -> tuple[bool, int, int]:
    """Resolve loop/interval/jitter from config.json's "schedule" section, with
    CLI flags (when explicitly passed) taking priority for quick one-off overrides."""
    schedule_cfg = config.get("schedule", {})

    loop = args.loop if args.loop is not None else bool(schedule_cfg.get("loop", False))
    interval_minutes = args.interval_minutes if args.interval_minutes is not None else schedule_cfg.get("interval_minutes", 60)
    jitter_minutes = args.jitter_minutes if args.jitter_minutes is not None else schedule_cfg.get("jitter_minutes", 15)

    return loop, interval_minutes, jitter_minutes


def get_log_server_settings(config: dict, args) -> tuple[bool, int]:
    """Resolve enabled/port for the web log viewer from config.json's "log_server"
    section, with CLI flags taking priority when explicitly passed."""
    log_cfg = config.get("log_server", {})

    enabled = log_cfg.get("enabled", True) and not bool(args.no_log_server)
    port = args.log_server_port if args.log_server_port is not None else log_cfg.get("port", 8080)

    return enabled, port


def get_discord_webhook_url(config: dict) -> Optional[str]:
    """Return the configured Discord webhook URL if notifications are enabled
    and a URL is set, else None."""
    discord_cfg = config.get("notifications", {}).get("discord", {})
    if not discord_cfg.get("enabled", False):
        return None
    return discord_cfg.get("webhook_url") or None


def _clean_diff_path(path: str) -> str:
    """Turn a DeepDiff path into a compact human-readable field path."""
    return (
        path
        .replace("root['data']", "data")
        .replace("root", "data")
        .replace("['", ".")
        .replace("']", "")
        .lstrip(".")
    )


def _format_discord_value(value) -> str:
    """Format a changed value safely for a Discord message."""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    return str(value).replace("`", "'")


def format_diff_discord(diff: Optional[dict], old_data: dict = None, new_data: dict = None) -> str:
    """Format actual changed values from DeepDiff for Discord."""
    if not diff:
        return "• No specific diff available."

    lines = []

    for path, change in diff.get("values_changed", {}).items():
        clean_path = _clean_diff_path(path)
        lines.append(f"• **{clean_path}**")
        lines.append(f"  Old: `{_format_discord_value(change.get('old_value'))}`")
        lines.append(f"  New: `{_format_discord_value(change.get('new_value'))}`")

    for path, change in diff.get("type_changes", {}).items():
        clean_path = _clean_diff_path(path)
        lines.append(f"• **{clean_path}** (type changed)")
        lines.append(f"  Old: `{_format_discord_value(change.get('old_value'))}`")
        lines.append(f"  New: `{_format_discord_value(change.get('new_value'))}`")

    for path in diff.get("dictionary_item_added", []):
        lines.append(f"• **Added:** `{_clean_diff_path(path)}`")

    for path in diff.get("dictionary_item_removed", []):
        lines.append(f"• **Removed:** `{_clean_diff_path(path)}`")

    for path, value in diff.get("iterable_item_added", {}).items():
        clean_path = _clean_diff_path(path)
        lines.append(f"• **Added item:** `{clean_path}`")
        lines.append(f"  Value: `{_format_discord_value(value)}`")

    for path, value in diff.get("iterable_item_removed", {}).items():
        clean_path = _clean_diff_path(path)
        lines.append(f"• **Removed item:** `{clean_path}`")
        lines.append(f"  Value: `{_format_discord_value(value)}`")

    if lines:
        return "\n".join(lines)

    if old_data is not None and new_data is not None:
        json_delta = format_json_delta(old_data, new_data)
        if json_delta:
            truncated = json_delta if len(json_delta) <= 1500 else json_delta[:1500] + "\n…"
            return "```diff\n" + truncated + "\n```"

    return "• No specific diff available."


def _send_discord_content(webhook_url: str, content: str) -> None:
    """Send one Discord webhook message."""
    payload = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f"Discord returned unexpected status {resp.status}")


def send_discord_notification(webhook_url: str, change_details: list[dict]) -> None:
    """POST actual changed values to Discord, splitting long messages safely."""
    if not change_details:
        return

    messages = []
    current_lines = [
        f"🔔 **USCIS case change(s) detected** ({len(change_details)} case(s))",
        "",
    ]

    def flush():
        nonlocal current_lines
        if current_lines:
            content = "\n".join(current_lines).strip()
            if content:
                messages.append(content)
        current_lines = []

    for item in change_details:
        case_lines = [f"**{item['nickname']}** ({item['case_number']})"]
        if item.get("account_name"):
            case_lines[0] += f"  [{item['account_name']}]"

        changes = item.get("changes", [])

        if not changes:
            # Backward-compatible fallback if an old change_details object
            # somehow reaches this function.
            changed = item.get("changed", [])
            if changed:
                case_lines.extend(["", f"Changed sources: {', '.join(changed)}"])
            else:
                case_lines.extend(["", "• No change details available."])

        for change in changes:
            case_lines.extend(["", f"**{change['label']}**"])
            try:
                diff_text = format_diff_discord(change.get("diff"), change.get("old_data"), change.get("new_data"))
            except Exception as e:
                diff_text = f"• (could not format diff: {e})"
            case_lines.extend(diff_text.splitlines())

        candidate = "\n".join(current_lines + case_lines)
        if len(candidate) > 1900 and current_lines:
            flush()

        # If one case is larger than Discord's limit, split it by lines.
        if len("\n".join(case_lines)) > 1900:
            for line in case_lines:
                if len("\n".join(current_lines + [line])) > 1900 and current_lines:
                    flush()
                if len(line) > 1900:
                    line = line[:1890] + "…"
                current_lines.append(line)
            current_lines.append("")
        else:
            current_lines.extend(case_lines)
            current_lines.append("")

    flush()

    try:
        for content in messages:
            _send_discord_content(webhook_url, content)
        print("Sent Discord notification")
    except Exception as e:
        # Never let a notification failure affect the actual watch results.
        print(f"Failed to send Discord notification (non-fatal): {e}")


def humanize_time_ago(timestamp_str: str) -> str:
    """Convert an ISO timestamp to a human-readable 'X hours and Y minutes ago' format"""
    try:
        if timestamp_str.endswith('Z'):
            timestamp_str = timestamp_str[:-1] + '+00:00'
        ts = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(ts.tzinfo)
        delta = now - ts

        total_seconds = int(delta.total_seconds())
        if total_seconds < 0:
            return "in the future"

        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60

        parts = []
        if days > 0:
            parts.append(f"{days} day{'s' if days != 1 else ''}")
        if hours > 0:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes > 0 and days == 0:
            parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")

        if not parts:
            return "just now"
        return " and ".join(parts[:2]) + " ago"
    except Exception:
        return timestamp_str


def slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug"""
    import re
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[-\s]+', '-', text)
    return text


def get_case_output_dir(nickname: str) -> Path:
    """Get the output directory for a specific case using nickname"""
    folder_name = slugify(nickname)
    case_dir = OUTPUT_DIR / folder_name
    case_dir.mkdir(parents=True, exist_ok=True)
    return case_dir


def save_latest(nickname: str, data: dict) -> Path:
    """Save the latest case data to latest.json"""
    case_dir = get_case_output_dir(nickname)
    latest_file = case_dir / "latest.json"
    with open(latest_file, "w") as f:
        json.dump(data, f, indent=2)
    return latest_file


def load_latest(nickname: str) -> Optional[dict]:
    """Load the previous case data from latest.json if it exists"""
    case_dir = get_case_output_dir(nickname)
    latest_file = case_dir / "latest.json"
    if not latest_file.exists():
        return None

    with open(latest_file, "r") as f:
        return json.load(f)


def save_receipt_info(nickname: str, data: dict) -> Path:
    """Save the receipt_info data to receipt_info.json"""
    case_dir = get_case_output_dir(nickname)
    receipt_info_file = case_dir / "receipt_info.json"
    with open(receipt_info_file, "w") as f:
        json.dump(data, f, indent=2)
    return receipt_info_file


def load_receipt_info(nickname: str) -> Optional[dict]:
    """Load the previous receipt_info data if it exists"""
    case_dir = get_case_output_dir(nickname)
    receipt_info_file = case_dir / "receipt_info.json"
    if not receipt_info_file.exists():
        return None

    with open(receipt_info_file, "r") as f:
        return json.load(f)


def save_documents(nickname: str, data: dict) -> Path:
    """Save the documents data to documents.json"""
    case_dir = get_case_output_dir(nickname)
    documents_file = case_dir / "documents.json"
    with open(documents_file, "w") as f:
        json.dump(data, f, indent=2)
    return documents_file


def load_documents(nickname: str) -> Optional[dict]:
    """Load the previous documents data if it exists"""
    case_dir = get_case_output_dir(nickname)
    documents_file = case_dir / "documents.json"
    if not documents_file.exists():
        return None

    with open(documents_file, "r") as f:
        return json.load(f)


def save_case_status(nickname: str, data: dict) -> Path:
    """Save the case_status data to case_status.json"""
    case_dir = get_case_output_dir(nickname)
    case_status_file = case_dir / "case_status.json"
    with open(case_status_file, "w") as f:
        json.dump(data, f, indent=2)
    return case_status_file


def load_case_status(nickname: str) -> Optional[dict]:
    """Load the previous case_status data if it exists"""
    case_dir = get_case_output_dir(nickname)
    case_status_file = case_dir / "case_status.json"
    if not case_status_file.exists():
        return None

    with open(case_status_file, "r") as f:
        return json.load(f)


def get_session_cookie_file(account_name: str) -> Path:
    """Get the cookie jar file path for a given account"""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return SESSION_DIR / f"{slugify(account_name)}_cookies.json"


def save_session_cookies(account_name: str, cookies: list[dict]) -> Path:
    """Persist the browser's cookies so a future run can skip the login flow"""
    cookie_file = get_session_cookie_file(account_name)
    payload = {
        "saved_at": datetime.now().isoformat(),
        "cookies": cookies,
    }
    with open(cookie_file, "w") as f:
        json.dump(payload, f, indent=2)
    return cookie_file


def load_session_cookies(account_name: str) -> Optional[list[dict]]:
    """Load previously saved cookies for an account, if any and not too old"""
    cookie_file = get_session_cookie_file(account_name)
    if not cookie_file.exists():
        return None

    with open(cookie_file, "r") as f:
        payload = json.load(f)

    saved_at_str = payload.get("saved_at")
    if saved_at_str:
        try:
            saved_at = datetime.fromisoformat(saved_at_str)
            age_seconds = (datetime.now() - saved_at).total_seconds()
            if age_seconds > SESSION_MAX_AGE_SECONDS:
                return None
        except ValueError:
            pass

    return payload.get("cookies")


def _current_timestamp_label() -> str:
    """Human-readable, zone-labeled timestamp, e.g. '2026-08-08 11:11:58 CDT'."""
    now = datetime.now()
    tz_label = time.strftime("%Z")
    return now.strftime("%Y-%m-%d %H:%M:%S") + (f" {tz_label}" if tz_label else "")


def _format_duration_seconds(total_seconds: float) -> str:
    """Format a duration in seconds as 'X days and Y hours', 'X hours and Y minutes', etc."""
    total_seconds = max(0, int(total_seconds))
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60

    parts = []
    if days > 0:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes > 0 and days == 0:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")

    if not parts:
        return "less than a minute"
    return " and ".join(parts[:2])


def _status_file_path(kind: str, account_name: str) -> Path:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return SESSION_DIR / f"{slugify(account_name)}_{kind}_status.json"


def _load_status(kind: str, account_name: str) -> Optional[dict]:
    """Load the single most recent status of the given kind for one account."""
    status_file = _status_file_path(kind, account_name)
    if not status_file.exists():
        return None
    try:
        with open(status_file, "r") as f:
            return json.load(f)
    except Exception:
        return None


def _save_status(kind: str, account_name: str, status: str, detail: str, extra: Optional[dict] = None) -> None:
    """Shared helper for recording a timestamped status file per account.
    `kind` becomes part of the filename, e.g. "login" -> *_login_status.json."""
    status_file = _status_file_path(kind, account_name)
    payload = {
        "account_name": account_name,
        "timestamp": _current_timestamp_label(),
        "raw_timestamp": datetime.now().isoformat(),  # for computing durations later - not displayed directly
        "status": status,  # "success" or "failed"
        "detail": detail,
    }
    if extra:
        payload.update(extra)
    try:
        with open(status_file, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception:
        pass  # best-effort - never let this break the actual watch flow


def _load_all_statuses(kind: str) -> list[dict]:
    """Load the most recent status of the given kind for every account that has one."""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    statuses = []
    for status_file in sorted(SESSION_DIR.glob(f"*_{kind}_status.json")):
        try:
            with open(status_file, "r") as f:
                statuses.append(json.load(f))
        except Exception:
            continue
    return statuses


def save_login_status(account_name: str, status: str, detail: str) -> None:
    """Record the outcome of the most recent login attempt (not every check
    cycle - only actual sign-in attempts, since sessions are reused otherwise).
    On a successful login, also records how long the previous session lasted -
    i.e. how long USCIS actually let the saved cookies/session stay valid
    before a fresh sign-in was required."""
    extra = {}
    if status == "success":
        previous = _load_status("login", account_name)
        if previous and previous.get("status") == "success" and previous.get("raw_timestamp"):
            try:
                prev_dt = datetime.fromisoformat(previous["raw_timestamp"])
                elapsed_seconds = (datetime.now() - prev_dt).total_seconds()
                if elapsed_seconds >= 0:
                    extra["session_duration"] = _format_duration_seconds(elapsed_seconds)
                    extra["previous_login_at"] = previous.get("timestamp")
            except ValueError:
                pass

    _save_status("login", account_name, status, detail, extra=extra)


def load_all_login_statuses() -> list[dict]:
    """Load the most recent login-attempt status for every account that has one"""
    return _load_all_statuses("login")


def save_fetch_status(account_name: str, status: str, detail: str) -> None:
    """Record the outcome of the most recent case-data API fetch (every check
    cycle updates this, whether or not a login was needed that cycle)."""
    _save_status("fetch", account_name, status, detail)


def load_all_fetch_statuses() -> list[dict]:
    """Load the most recent fetch status for every account that has one"""
    return _load_all_statuses("fetch")


def load_silent_updates(nickname: str) -> list[str]:
    """Load silent update timestamps from silent_updates.json"""
    case_dir = get_case_output_dir(nickname)
    silent_updates_file = case_dir / "silent_updates.json"
    if not silent_updates_file.exists():
        return []

    with open(silent_updates_file, "r") as f:
        data = json.load(f)
        return data.get("silent_updates", [])


def save_silent_update(nickname: str, timestamp: str) -> Path:
    """Append a new silent update timestamp to silent_updates.json"""
    case_dir = get_case_output_dir(nickname)
    silent_updates_file = case_dir / "silent_updates.json"

    silent_updates = load_silent_updates(nickname)
    silent_updates.append(timestamp)

    data = {"silent_updates": silent_updates}
    with open(silent_updates_file, "w") as f:
        json.dump(data, f, indent=2)

    return silent_updates_file


def is_silent_update(diff: dict) -> bool:
    """
    Check if a diff represents a silent update.
    A silent update is when ONLY updatedAt and/or updatedAtTimestamp changed.
    """
    if not diff:
        return False

    if "values_changed" in diff:
        for path in diff["values_changed"].keys():
            if "['updatedAt']" not in path and "['updatedAtTimestamp']" not in path:
                return False

    for key in diff.keys():
        if key == "values_changed":
            continue
        if key in ["dictionary_item_added", "dictionary_item_removed"]:
            for path in diff[key]:
                if "['error']" not in path:
                    return False
        else:
            return False

    return True


# Registry of data sources with their load/save functions
DATA_SOURCES = {
    "case_details": {
        "load": load_latest,
        "save": save_latest,
        "label": "Case details",
    },
    "receipt_info": {
        "load": load_receipt_info,
        "save": save_receipt_info,
        "label": "Receipt info",
    },
    "documents": {
        "load": load_documents,
        "save": save_documents,
        "label": "Documents",
    },
    "case_status": {
        "load": load_case_status,
        "save": save_case_status,
        "label": "Case status",
    },
}


def process_data_source(
    source_key: str,
    nickname: str,
    case_number: str,
    new_data: dict,
    dry_run: bool = False,
) -> tuple[bool, Optional[dict], Optional[dict]]:
    """
    Process a single data source: compare with old data, detect changes, save, and log.
    Returns (has_changes, diff, old_data).
    """
    source = DATA_SOURCES[source_key]
    label = source["label"]
    load_fn = source["load"]
    save_fn = source["save"]

    old_data = load_fn(nickname)
    has_changes = False
    diff = None

    if old_data:
        diff = DeepDiff(old_data, new_data, ignore_order=True)
        if diff:
            has_changes = True
            if source_key == "case_details":
                if is_silent_update(diff):
                    print(f"  {nickname}: Silent update detected (updatedAt timestamp changed)")
                    if not dry_run:
                        timestamp = datetime.now().isoformat() + "Z"
                        save_silent_update(nickname, timestamp)
                        changelog_path = append_changelog(nickname, case_number, diff, old_data, new_data)
                else:
                    print_change_alert(nickname, case_number, diff, old_data, new_data)
                    if not dry_run:
                        changelog_path = append_changelog(nickname, case_number, diff, old_data, new_data)
                        print(f"  Changelog updated: {changelog_path}")
            elif source_key == "receipt_info":
                old_loc = old_data.get("data", {}).get("receipt_details", {}).get("location") if old_data.get("data") else None
                new_loc = new_data.get("data", {}).get("receipt_details", {}).get("location") if new_data.get("data") else None
                if old_loc != new_loc:
                    print(f"  {nickname}: {label} location changed: {old_loc} -> {new_loc}")
                else:
                    print(f"  {nickname}: {label} changed")
                if not dry_run:
                    append_changelog(nickname, case_number, diff, old_data, new_data)
            else:
                print(f"  {nickname}: {label} changed")
                if not dry_run:
                    append_changelog(nickname, case_number, diff, old_data, new_data)
        elif source_key == "case_details":
            print(f"  {nickname}: No changes")
    else:
        has_changes = True
        if source_key == "case_details":
            print(f"  {nickname}: First run - recording initial data")
            if not dry_run:
                create_initial_changelog(nickname, case_number, new_data)
        elif source_key == "receipt_info":
            loc = new_data.get("data", {}).get("receipt_details", {}).get("location") if new_data.get("data") else None
            if loc:
                print(f"  {nickname}: {label} recorded (location: {loc})")
        elif new_data.get("data"):
            print(f"  {nickname}: {label} recorded")

    if not dry_run:
        save_fn(nickname, new_data)

    return has_changes, diff, old_data


def format_json_delta(old_data: dict, new_data: dict) -> str:
    """Format a JSON delta showing added (+) and removed (-) lines"""
    old_lines = json.dumps(old_data, indent=2).splitlines()
    new_lines = json.dumps(new_data, indent=2).splitlines()

    differ = difflib.unified_diff(old_lines, new_lines, lineterm='')

    delta_lines = []
    for line in differ:
        if line.startswith('---') or line.startswith('+++'):
            continue
        if line.startswith('@@'):
            continue
        delta_lines.append(line)

    return "\n".join(delta_lines) if delta_lines else ""


def format_diff(diff: dict, old_data: dict, new_data: dict) -> str:
    """Format a DeepDiff result into a readable markdown string"""
    lines = []

    if "values_changed" in diff:
        for path, change in diff["values_changed"].items():
            lines.append(f"- **{path}**")
            lines.append(f"  - Old: `{change['old_value']}`")
            lines.append(f"  - New: `{change['new_value']}`")

    if "dictionary_item_added" in diff:
        for path in diff["dictionary_item_added"]:
            lines.append(f"- **Added** {path}")

    if "dictionary_item_removed" in diff:
        for path in diff["dictionary_item_removed"]:
            lines.append(f"- **Removed** {path}")

    if "iterable_item_added" in diff:
        for path, value in diff["iterable_item_added"].items():
            lines.append(f"- **Added item** {path}: `{value}`")

    if "iterable_item_removed" in diff:
        for path, value in diff["iterable_item_removed"].items():
            lines.append(f"- **Removed item** {path}: `{value}`")

    json_delta = format_json_delta(old_data, new_data)
    if json_delta:
        lines.append("")
        lines.append("**JSON Delta:**")
        lines.append("```diff")
        lines.append(json_delta)
        lines.append("```")

    return "\n".join(lines) if lines else "No specific changes detected"


def format_diff_console(diff: dict, old_data: dict = None, new_data: dict = None) -> str:
    """Format a DeepDiff result for console output"""
    lines = []

    if "values_changed" in diff:
        for path, change in diff["values_changed"].items():
            clean_path = path.replace("root['data']", "").replace("['", ".").replace("']", "").lstrip(".")
            lines.append(f"    {clean_path}:")
            lines.append(f"      - {change['old_value']}")
            lines.append(f"      + {change['new_value']}")

    if "dictionary_item_added" in diff:
        for path in diff["dictionary_item_added"]:
            clean_path = path.replace("root['data']", "").replace("['", ".").replace("']", "").lstrip(".")
            lines.append(f"    + Added: {clean_path}")

    if "dictionary_item_removed" in diff:
        for path in diff["dictionary_item_removed"]:
            clean_path = path.replace("root['data']", "").replace("['", ".").replace("']", "").lstrip(".")
            lines.append(f"    - Removed: {clean_path}")

    if "iterable_item_added" in diff:
        for path, value in diff["iterable_item_added"].items():
            clean_path = path.replace("root['data']", "").replace("['", ".").replace("']", "").lstrip(".")
            lines.append(f"    + New item in {clean_path}")

    if "iterable_item_removed" in diff:
        for path, value in diff["iterable_item_removed"].items():
            clean_path = path.replace("root['data']", "").replace("['", ".").replace("']", "").lstrip(".")
            lines.append(f"    - Removed item from {clean_path}")

    if old_data is not None and new_data is not None:
        json_delta = format_json_delta(old_data, new_data)
        if json_delta:
            lines.append("")
            lines.append("    JSON Delta:")
            for delta_line in json_delta.splitlines():
                lines.append(f"    {delta_line}")

    return "\n".join(lines) if lines else "    (details unavailable)"


def append_changelog(nickname: str, case_number: str, diff: dict, old_data: dict, new_data: dict) -> Path:
    """Append a change entry to the changelog markdown file"""
    case_dir = get_case_output_dir(nickname)
    changelog_file = case_dir / "changelog.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not changelog_file.exists():
        with open(changelog_file, "w") as f:
            f.write(f"# USCIS Case Changelog: {nickname}\n\n")
            f.write(f"**Case Number:** {case_number}\n\n")
            f.write("This file tracks all changes detected in your USCIS case.\n\n")
            f.write("---\n\n")

    with open(changelog_file, "a") as f:
        f.write(f"## {timestamp}\n\n")
        f.write(format_diff(diff, old_data, new_data))
        f.write("\n\n---\n\n")

    return changelog_file


def create_initial_changelog(nickname: str, case_number: str, data: dict = None) -> Path:
    """Create initial changelog entry for first run"""
    case_dir = get_case_output_dir(nickname)
    changelog_file = case_dir / "changelog.md"

    with open(changelog_file, "w") as f:
        f.write(f"# USCIS Case Changelog: {nickname}\n\n")
        f.write(f"**Case Number:** {case_number}\n\n")
        f.write("This file tracks all changes detected in your USCIS case.\n\n")
        f.write("---\n\n")
        f.write(f"## {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - Initial fetch\n\n")
        f.write("First case data recorded.\n\n")

        if data and "data" in data:
            inner = data["data"]

            updated_at = inner.get("updatedAtTimestamp")
            if updated_at:
                time_ago = humanize_time_ago(updated_at)
                f.write(f"**Last Updated:** {updated_at} ({time_ago})\n\n")

            events = inner.get("events", [])
            if events:
                f.write(f"**Events ({len(events)}):**\n")
                for event in events:
                    event_code = event.get("eventCode", "Unknown")
                    event_time = event.get("createdAtTimestamp", "")
                    time_ago = humanize_time_ago(event_time) if event_time else ""
                    f.write(f"- `{event_code}` - {event_time} ({time_ago})\n")
                f.write("\n")

            notices = inner.get("notices", [])
            if notices:
                f.write(f"**Notices ({len(notices)}):**\n")
                for notice in notices:
                    action_type = notice.get("actionType", "Unknown")
                    gen_date = notice.get("generationDate", "")
                    time_ago = humanize_time_ago(gen_date) if gen_date else ""
                    f.write(f"- {action_type} - {gen_date} ({time_ago})\n")
                f.write("\n")

        f.write("---\n\n")

    return changelog_file


def detect_important_changes(old_data: dict, new_data: dict) -> list[str]:
    """Detect important changes and return human-readable descriptions"""
    messages = []

    old_inner = old_data.get("data", {})
    new_inner = new_data.get("data", {})

    old_updated = old_inner.get("updatedAtTimestamp")
    new_updated = new_inner.get("updatedAtTimestamp")
    if old_updated != new_updated and new_updated:
        time_ago = humanize_time_ago(new_updated)
        messages.append(f"Case updated {time_ago}")

    old_events = {e.get("eventId"): e for e in old_inner.get("events", [])}
    new_events = new_inner.get("events", [])
    for event in new_events:
        event_id = event.get("eventId")
        if event_id and event_id not in old_events:
            event_code = event.get("eventCode", "Unknown")
            event_time = event.get("createdAtTimestamp", "")
            time_ago = humanize_time_ago(event_time) if event_time else ""
            messages.append(f"New '{event_code}' event added {time_ago}")

    old_notices = {n.get("letterId"): n for n in old_inner.get("notices", [])}
    new_notices = new_inner.get("notices", [])
    for notice in new_notices:
        letter_id = notice.get("letterId")
        if letter_id and letter_id not in old_notices:
            action_type = notice.get("actionType", "Unknown")
            gen_date = notice.get("generationDate", "")
            time_ago = humanize_time_ago(gen_date) if gen_date else ""
            messages.append(f"New notice: '{action_type}' {time_ago}")

    return messages


def print_change_alert(nickname: str, case_number: str, diff: dict, old_data: dict = None, new_data: dict = None):
    """Print a prominent alert when changes are detected"""
    print("\n" + "!" * 60)
    print("!" * 60)
    print(f"!!!  CHANGE DETECTED: {nickname}")
    print(f"!!!  Case: {case_number}")
    print("!" * 60)

    if old_data and new_data:
        important = detect_important_changes(old_data, new_data)
        if important:
            print("\n  Summary:")
            for msg in important:
                print(f"    -> {msg}")

    print("\n  Details:")
    print(format_diff_console(diff, old_data, new_data))
    print("\n" + "!" * 60)
    print("!" * 60 + "\n")


class USCISWatcher:
    def __init__(self, account: dict, browser_config: dict, verbose: bool = False, force_login: bool = False):
        self.username = account["username"]
        self.password = account["password"]
        self.totp_secret = account["totp_secret"]
        self.account_name = account.get("name", "default")
        self.cases = account["cases"]
        self.browser_config = browser_config
        self.driver = None
        self.verbose = verbose
        self.force_login = force_login

    def log(self, message: str):
        """Print message only in verbose mode"""
        if self.verbose:
            print(f">>> [{self.account_name}] {message}")

    def _setup_driver(self):
        """Initialize the Chrome driver"""
        options = Options()

        headless = self.browser_config.get("headless", False)
        if headless:
            options.add_argument("--headless=new")

        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-software-rasterizer")
        options.add_argument("--window-size=1400,900")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        try:
            self.driver = webdriver.Chrome(options=options)
        except (SessionNotCreatedException, WebDriverException) as e:
            if not headless and os.environ.get("DOCKER_CONTAINER") == "1":
                raise RuntimeError(
                    "Chrome failed to start. You're running in Docker (no display server) "
                    "but browser.headless is false/unset in config.json - set it to true."
                ) from e
            raise
        self.driver.implicitly_wait(10)

    def ensure_session(self) -> None:
        """Make sure the browser is open and pointed at the case portal."""
        if self.driver:
            return

        self._setup_driver()
        self._load_saved_cookies()
        self.driver.get("https://my.uscis.gov/account")
        time.sleep(2)

    def _load_saved_cookies(self) -> bool:
        """Best-effort: drop previously saved cookies into the browser."""
        if self.force_login:
            return False

        cookies = load_session_cookies(self.account_name)
        if not cookies:
            return False

        self.log("Found saved session cookies, loading them...")

        cookies_by_domain: dict[str, list[dict]] = {}
        for cookie in cookies:
            domain = cookie.get("domain", "").lstrip(".")
            if not domain:
                continue
            cookies_by_domain.setdefault(domain, []).append(cookie)

        for domain, domain_cookies in cookies_by_domain.items():
            self.driver.get(f"https://{domain}/")
            for cookie in domain_cookies:
                clean_cookie = {
                    k: v for k, v in cookie.items()
                    if k in ("name", "value", "path", "domain", "secure", "httpOnly", "expiry", "sameSite")
                }
                try:
                    self.driver.add_cookie(clean_cookie)
                except Exception as e:
                    self.log(f"Skipping cookie {cookie.get('name')}: {e}")

        return True

    def _safe_current_url(self) -> str:
        try:
            return self.driver.current_url
        except Exception:
            return "(unknown)"

    def _save_debug_snapshot(self, label: str, max_kept: int = 20) -> None:
        """Capture a screenshot + page source when login doesn't go as expected."""
        try:
            debug_dir = OUTPUT_DIR / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = debug_dir / f"{slugify(self.account_name)}_{label}_{stamp}"

            current_url = self._safe_current_url()
            try:
                title = self.driver.title
            except Exception:
                title = "(unknown)"
            self.log(f"Login did not complete. URL: {current_url!r} Title: {title!r}")

            self.driver.save_screenshot(f"{base}.png")
            with open(f"{base}.html", "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            self.log(f"Saved debug snapshot: {base}.png / {base}.html")

            existing = sorted(debug_dir.glob(f"{slugify(self.account_name)}_{label}_*.png"))
            for old_png in existing[:-max_kept]:
                old_png.unlink(missing_ok=True)
                old_html = old_png.with_suffix(".html")
                old_html.unlink(missing_ok=True)
        except Exception as e:
            self.log(f"Could not save debug snapshot: {e}")

    def _verify_login_via_api(self) -> bool:
        """Fallback check when neither expected URL shows up: navigate to the case
        portal and try one real API call. This is a more reliable signal than
        guessing at URL patterns, since USCIS's exact post-login URL has changed
        before without warning."""
        if not self.cases:
            return False

        case_number = self.cases[0]["case_number"]
        try:
            self.driver.get("https://my.uscis.gov/account")
            time.sleep(2)

            url = f"https://my.uscis.gov/account/case-service/api/cases/{case_number}"
            status = self.driver.execute_async_script(f"""
                var callback = arguments[arguments.length - 1];
                fetch("{url}", {{ method: 'GET', credentials: 'include' }})
                    .then(response => callback(response.status))
                    .catch(error => callback(-1));
            """)
            self.log(f"API verification check for {case_number} returned status {status}")
            return isinstance(status, int) and 200 <= status < 300
        except Exception as e:
            self.log(f"API verification check failed: {e}")
            return False

    def _full_login(self) -> None:
        """Log into USCIS account interactively (credentials + TOTP)"""
        self.log("Navigating to login page...")
        self.driver.get("https://myaccount.uscis.gov/sign-in")

        wait = WebDriverWait(self.driver, 30)

        self.log("Entering credentials...")
        email_field = wait.until(
            EC.presence_of_element_located((By.ID, "email-address"))
        )
        email_field.send_keys(self.username)

        password_field = self.driver.find_element(By.ID, "password")
        password_field.send_keys(self.password)

        self.log("Signing in...")
        sign_in_button = wait.until(
            EC.element_to_be_clickable((By.ID, "sign-in-btn"))
        )
        sign_in_button.click()

        self.log("Entering OTP...")
        otp_field = wait.until(
            EC.presence_of_element_located((By.ID, "secure-verification-code"))
        )

        otp_code = TOTP(self.totp_secret).now()
        otp_field.send_keys(otp_code)

        submit_button = wait.until(
            EC.element_to_be_clickable((By.ID, "2fa-submit-btn"))
        )
        submit_button.click()

        self.log("Waiting for login to complete...")
        login_succeeded = False
        success_detail = ""
        try:
            # Accept either URL pattern - USCIS has changed which one shows up before
            wait.until(lambda d: (
                "uscis.gov/account" in d.current_url.lower()
                or "myaccount.uscis.gov/dashboard" in d.current_url.lower()
            ))
            time.sleep(2)
            login_succeeded = True
            success_detail = f"Landed on {self.driver.current_url}"
            self.log(f"Login successful! ({success_detail})")
        except TimeoutException:
            self.log(f"Neither expected URL appeared (current URL: {self._safe_current_url()!r}). "
                      f"Falling back to an API check before giving up...")

        if not login_succeeded:
            if self._verify_login_via_api():
                login_succeeded = True
                success_detail = "Verified via direct API call (URL didn't match, but the API confirmed we're authenticated)"
                self.log("Login verified via API call despite the URL not matching.")
            else:
                self._save_debug_snapshot("login_timeout")
                failure_detail = (
                    f"Neither URL matched nor did a direct API call succeed "
                    f"(current URL: {self._safe_current_url()!r})"
                )
                save_login_status(self.account_name, "failed", failure_detail)
                raise RuntimeError(
                    f"Login failed - {failure_detail}. A screenshot and page "
                    f"source were saved to output/debug/ for troubleshooting."
                )

        save_login_status(self.account_name, "success", success_detail)

        if "uscis.gov/account" not in self.driver.current_url.lower():
            self.driver.get("https://my.uscis.gov/account")
            time.sleep(3)

    def _save_session(self) -> None:
        """Persist cookies so future runs can skip login flow if unclosed"""
        cookies = self.driver.get_cookies()
        save_session_cookies(self.account_name, cookies)
        self.log(f"Saved {len(cookies)} cookies for future runs")

    def fetch_all_case_data(self, case_number: str) -> dict:
        """Fetch all case data from multiple APIs in parallel"""
        self.ensure_session()

        apis = {
            "case_details": f"https://my.uscis.gov/account/case-service/api/cases/{case_number}",
            "receipt_info": f"https://my.uscis.gov/secure-messaging/api/case-service/receipt_info/{case_number}",
            "documents": f"https://my.uscis.gov/account/case-service/api/cases/{case_number}/documents",
            "case_status": f"https://my.uscis.gov/account/case-service/api/case_status/{case_number}",
        }

        api_entries = ", ".join([f'["{key}", "{url}"]' for key, url in apis.items()])

        result = self.driver.execute_async_script(f"""
            var callback = arguments[arguments.length - 1];
            var apis = [{api_entries}];

            Promise.all(apis.map(([key, url]) =>
                fetch(url, {{
                    method: 'GET',
                    credentials: 'include'
                }})
                .then(response => response.text().then(text => ({{
                    key: key,
                    status: response.status,
                    body: text
                }})))
                .catch(error => ({{
                    key: key,
                    error: error.message
                }}))
            ))
            .then(results => {{
                var output = {{}};
                results.forEach(r => {{
                    output[r.key] = r;
                }});
                callback(JSON.stringify(output));
            }})
            .catch(error => callback(JSON.stringify({{"error": error.message}})));
        """)

        if not result:
            raise Exception(f"Failed to fetch case data for {case_number}")

        raw_results = json.loads(result)
        if "error" in raw_results:
            raise Exception(f"Fetch error: {raw_results['error']}")

        processed = {}
        for key, data in raw_results.items():
            if "error" in data:
                self.log(f"{key} error [{case_number}]: {data['error']}")
                processed[key] = {"data": None, "error": data['error']}
            elif data.get("status") != 200:
                self.log(f"{key} status [{case_number}]: {data.get('status')}")
                processed[key] = {"data": None, "error": f"Status {data.get('status')}"}
            else:
                try:
                    processed[key] = json.loads(data["body"])
                except json.JSONDecodeError:
                    processed[key] = {"data": None, "error": "Invalid JSON response"}

        return processed

    def _needs_login(self, all_data: dict) -> bool:
        """Check whether the case-details response indicates we're not authenticated"""
        case_details = all_data.get("case_details", {})
        if not isinstance(case_details, dict):
            return False
        return bool(case_details.get("error"))

    def process_all_cases(self, dry_run: bool = False) -> tuple[int, set[str], list[dict]]:
        """Process all cases for this account."""
        self.ensure_session()

        changes_detected = 0
        changed_nicknames = set()
        change_details = []
        session_confirmed = False

        for case in self.cases:
            case_number = case["case_number"]
            nickname = case["nickname"]

            try:
                all_data = self.fetch_all_case_data(case_number)

                if not session_confirmed and self._needs_login(all_data):
                    self.log(f"Case details request for {nickname} failed - logging in...")
                    self._full_login()
                    self._save_session()
                    all_data = self.fetch_all_case_data(case_number)

                session_confirmed = True

                # Record fetch outcome every cycle - independent of login status,
                # since a cycle can fetch successfully without ever needing to log in
                if self._needs_login(all_data):
                    fetch_error = all_data.get("case_details", {}).get("error", "unknown error")
                    save_fetch_status(self.account_name, "failed", f"{nickname} ({case_number}): {fetch_error}")
                else:
                    save_fetch_status(self.account_name, "success", f"{nickname} ({case_number})")

                changes = []

                for source_key in DATA_SOURCES:
                    new_data = all_data.get(source_key, {})
                    has_changes, diff, old_data = process_data_source(
                        source_key, nickname, case_number, new_data, dry_run
                    )

                    if has_changes:
                        changes.append({
                            "label": DATA_SOURCES[source_key]["label"],
                            "diff": diff,
                            "old_data": old_data,
                            "new_data": new_data,
                        })

                        if source_key == "case_details" and diff:
                            changes_detected += 1

                if changes:
                    changed_nicknames.add(slugify(nickname))
                    change_details.append({
                        "account_name": self.account_name,
                        "nickname": nickname,
                        "case_number": case_number,
                        "changes": changes,
                    })

            except Exception as e:
                print(f"  {nickname}: ERROR - {e}")
                save_fetch_status(self.account_name, "failed", f"{nickname} ({case_number}): {e}")

        return changes_detected, changed_nicknames, change_details

    def close(self):
        """Close the browser"""
        if self.driver:
            self.driver.quit()
            self.driver = None


def simulate_diff():
    """Simulate a diff by modifying the existing latest.json temporarily"""
    print("=" * 60)
    print("SIMULATION MODE - Testing diff detection")
    print("=" * 60)

    config = load_config()
    accounts = config.get("accounts", [])
    if not accounts or not accounts[0].get("cases"):
        print("No cases configured. Add cases to config.json first.")
        return

    first_case = accounts[0]["cases"][0]
    nickname = first_case["nickname"]
    case_number = first_case["case_number"]

    case_dir = get_case_output_dir(nickname)
    latest_file = case_dir / "latest.json"

    if not latest_file.exists():
        print(f"No latest.json found for {nickname}. Run the watcher first to get initial data.")
        return

    with open(latest_file, "r") as f:
        current_data = json.load(f)

    simulated_new = copy.deepcopy(current_data)

    if "data" in simulated_new:
        data = simulated_new["data"]

        data["updatedAt"] = "2025-12-30"
        data["updatedAtTimestamp"] = "2025-12-30T15:30:00.000Z"

        new_event = {
            "receiptNumber": case_number,
            "eventId": "simulated-event-001",
            "eventCode": "APPR",
            "createdAt": "2025-12-30",
            "createdAtTimestamp": "2025-12-30T15:30:00.000Z",
            "updatedAt": "2025-12-30",
            "updatedAtTimestamp": "2025-12-30T15:30:00.000Z",
            "eventDateTime": "2025-12-30",
            "eventTimestamp": "2025-12-30T15:30:00.000Z"
        }
        if "events" in data:
            data["events"].insert(0, new_event)

        new_notice = {
            "receiptNumber": case_number,
            "letterId": "999999999",
            "generationDate": "2025-12-30T15:30:00.000Z",
            "actionType": "Case Approved"
        }
        if "notices" in data:
            data["notices"].insert(0, new_notice)

    diff = DeepDiff(current_data, simulated_new, ignore_order=True)

    print(f"\nSimulating changes for: {nickname} ({case_number})")
    print("-" * 60)

    if diff:
        print_change_alert(nickname, case_number, diff, current_data, simulated_new)

        print("\nChangelog entry that would be written:")
        print("-" * 40)
        print(format_diff(diff, current_data, simulated_new))
        print("-" * 40)
    else:
        print("No diff detected (this shouldn't happen in simulation)")

    print("\n" + "=" * 60)
    print("SIMULATION COMPLETE - No actual changes were saved")
    print("=" * 60)


def check_clock_drift(url: str = "https://www.google.com", warn_threshold_seconds: int = 20) -> None:
    """Compare this machine's clock against a server's Date header."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            server_date_header = resp.headers.get("Date")

        if not server_date_header:
            print("Clock check: server response had no Date header, skipping")
            return

        server_time = parsedate_to_datetime(server_date_header)
        if server_time.tzinfo is None:
            server_time = server_time.replace(tzinfo=timezone.utc)
        local_time = datetime.now(timezone.utc)
        drift_seconds = (local_time - server_time).total_seconds()

        if abs(drift_seconds) >= warn_threshold_seconds:
            print(f"WARNING: clock drift detected - this machine is {drift_seconds:+.1f}s off from "
                  f"real time. TOTP codes are time-based, so drift this large can cause login "
                  f"OTP failures. Check the host's NTP time sync.")
        else:
            print(f"Clock check OK ({drift_seconds:+.1f}s drift)")
    except Exception as e:
        print(f"Clock drift check failed (non-fatal): {e}")


def run_once(args, watcher: Optional[USCISWatcher] = None) -> tuple[int, Optional[USCISWatcher]]:
    """Run a single check pass across all configured accounts. Returns (total_changes, active_watcher)."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\nUSCIS Watcher - {timestamp}")

    config = load_config()
    browser_config = config.get("browser", {})
    accounts = config.get("accounts", [])

    if not accounts:
        print("Error: No accounts configured in config.json")
        return 0, watcher

    check_clock_drift()

    total_changes = 0
    all_changed_nicknames = set()
    all_change_details = []

    for account in accounts:
        account_name = account.get("name", "default")
        print(f"\nAccount: {account_name}")
        print("-" * 40)

        # Reuse existing browser instance across check loops if supplied
        if watcher is None:
            watcher = USCISWatcher(account, browser_config, verbose=args.verbose, force_login=args.fresh_login)

        try:
            changes, changed_nicknames, change_details = watcher.process_all_cases(dry_run=args.dry_run)
            total_changes += changes
            all_changed_nicknames.update(changed_nicknames)
            all_change_details.extend(change_details)
        except Exception as e:
            print(f"Error processing account {account_name}: {e}")
            if watcher:
                watcher.close()
                watcher = None

    print("\n" + "=" * 40)
    if total_changes > 0:
        print(f"!!! {total_changes} CHANGE(S) DETECTED !!!")
    else:
        print("All cases checked - no changes")
    print("=" * 40)

    print_summary(all_changed_nicknames if all_changed_nicknames else None)

    if all_change_details and not args.dry_run:
        webhook_url = get_discord_webhook_url(config)
        if webhook_url:
            send_discord_notification(webhook_url, all_change_details)

    return total_changes, watcher


class GracefulStop:
    """Tracks whether we've been asked to shut down (SIGTERM/SIGINT)"""

    def __init__(self):
        self.stop_requested = False
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT, self._handle)

    def _handle(self, signum, frame):
        print(f"\nReceived signal {signum}, will stop after the current check completes...")
        self.stop_requested = True

    def sleep(self, seconds: int):
        """Sleep in 1-second increments so a stop request is honored promptly."""
        for _ in range(seconds):
            if self.stop_requested:
                return
            time.sleep(1)


class _PrintToLogger:
    """Makes plain print() calls go through logging."""

    def __init__(self, logger: logging.Logger, level: int):
        self.logger = logger
        self.level = level
        self._buffer = ""

    def write(self, message: str):
        self._buffer += message
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self.logger.log(self.level, line)

    def flush(self):
        pass


def setup_logging() -> Path:
    """Route print() output to console and rotating file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("uscis_watcher")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.__stdout__)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console_handler)

    sys.stdout = _PrintToLogger(logger, logging.INFO)
    return LOG_FILE


def _tail_file(path: Path, n_lines: int) -> str:
    if not path.exists():
        return "(no logs yet)"
    with open(path, "r", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[-n_lines:])


def _render_status_html(statuses: list[dict], empty_message: str) -> str:
    """Shared renderer for a list of status dicts (login or fetch)."""
    import html as _html

    if not statuses:
        return f'<div class="status-box status-none">{_html.escape(empty_message)}</div>'

    rows = []
    for s in statuses:
        account = _html.escape(s.get("account_name", "unknown"))
        ts = _html.escape(s.get("timestamp", "unknown"))
        status = s.get("status", "unknown")
        detail = _html.escape(s.get("detail", ""))
        badge = '<span class="ok">✅ SUCCESS</span>' if status == "success" else '<span class="fail">❌ FAILED</span>'

        session_line = ""
        if s.get("session_duration"):
            duration = _html.escape(s["session_duration"])
            prev_at = _html.escape(s.get("previous_login_at", ""))
            session_line = f'<div class="session">Previous session lasted <strong>{duration}</strong> (logged in at {prev_at})</div>'

        rows.append(
            f'<div class="status-box status-{status}">'
            f'<div><strong>{account}</strong> — {badge}</div>'
            f'<div class="ts">{ts}</div>'
            f'<div class="detail">{detail}</div>'
            f'{session_line}'
            f'</div>'
        )
    return "\n".join(rows)


def _render_log_viewer_page(log_file: Path, tail_lines: int = 200) -> str:
    """Build the full log viewer page server-side. No JS polling - refresh the
    page (or hit the Refresh link) to pull the latest state."""
    import html as _html

    login_status_html = _render_status_html(
        load_all_login_statuses(),
        "No login attempts recorded yet (sessions are reused across checks - this only updates when a real sign-in happens).",
    )
    fetch_status_html = _render_status_html(
        load_all_fetch_statuses(),
        "No case-data fetches recorded yet.",
    )
    log_text = _html.escape(_tail_file(log_file, tail_lines))
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!doctype html>
<html>
<head>
<title>USCIS Watcher</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ background:#111; color:#ddd; font-family: ui-monospace, monospace; margin:0; padding:1rem 1rem 3rem; }}
  h2 {{ font-size:1rem; color:#aaa; margin: 1.25rem 0 0.5rem; }}
  a.refresh {{ color:#8ab4f8; text-decoration:none; font-size:0.85rem; }}
  #generated {{ color:#666; font-size:0.75rem; margin-bottom:0.75rem; }}
  .status-box {{ border:1px solid #333; border-radius:6px; padding:0.6rem 0.8rem; margin-bottom:0.6rem; font-size:0.85rem; }}
  .status-success {{ border-color:#2d5a2d; }}
  .status-failed {{ border-color:#5a2d2d; }}
  .ok {{ color:#7fdc7f; }}
  .fail {{ color:#e28080; }}
  .ts {{ color:#888; font-size:0.75rem; margin-top:0.15rem; }}
  .detail {{ color:#bbb; font-size:0.8rem; margin-top:0.3rem; word-break:break-word; }}
  .session {{ color:#c9a86a; font-size:0.8rem; margin-top:0.3rem; }}
  pre {{ white-space: pre-wrap; word-break: break-word; font-size:0.8rem; line-height:1.4; background:#0a0a0a; border:1px solid #333; border-radius:6px; padding:0.75rem; }}
  .loglink {{ font-size:0.8rem; color:#888; }}
</style>
</head>
<body>
<div id="generated">Generated {generated_at} — <a class="refresh" href="/">Refresh</a></div>

<h2>Login status</h2>
{login_status_html}

<h2>API fetch status</h2>
{fetch_status_html}

<h2>Recent logs (last {tail_lines} lines) <span class="loglink">— <a class="refresh" href="/logs.txt?lines=5000">view full log</a></span></h2>
<pre>{log_text}</pre>
</body>
</html>
"""


def _make_log_request_handler(log_file: Path, auth_user: Optional[str], auth_pass: Optional[str]):
    expected_auth = None
    if auth_user:
        expected_auth = "Basic " + base64.b64encode(f"{auth_user}:{auth_pass}".encode()).decode()

    class LogRequestHandler(BaseHTTPRequestHandler):
        def _authorized(self) -> bool:
            if expected_auth is None:
                return True
            if self.headers.get("Authorization") == expected_auth:
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="uscis-watcher-logs"')
            self.end_headers()
            return False

        def do_GET(self):
            if not self._authorized():
                return

            parsed = urlparse(self.path)
            if parsed.path == "/logs.txt":
                lines = 500
                try:
                    lines = int(parse_qs(parsed.query).get("lines", ["500"])[0])
                except ValueError:
                    pass
                lines = max(1, min(lines, 5000))

                body = _tail_file(log_file, lines).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path in ("/", ""):
                body = _render_log_viewer_page(log_file).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    return LogRequestHandler


def start_log_server(log_file: Path, port: int) -> ThreadingHTTPServer:
    """Start the web-based log viewer in a background thread."""
    auth_user = os.environ.get("LOG_SERVER_USER")
    auth_pass = os.environ.get("LOG_SERVER_PASS")

    if not auth_user:
        print(f"WARNING: log viewer on port {port} has no authentication. "
              f"Set LOG_SERVER_USER and LOG_SERVER_PASS env vars to add a login.")

    handler_cls = _make_log_request_handler(log_file, auth_user, auth_pass)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Log viewer listening on port {port} (open http://<this-host>:{port}/ from another device)")
    return server


def main():
    parser = argparse.ArgumentParser(description="USCIS Case Watcher")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--simulate", action="store_true", help="Simulate a diff to test change detection")
    parser.add_argument("--dry-run", action="store_true", help="Check for changes without saving")
    parser.add_argument("--fresh-login", action="store_true", help="Ignore any saved session and log in from scratch")
    parser.add_argument("--loop", action="store_true", default=None,
                         help="Run forever, checking on a schedule. Overrides config.json's schedule.loop.")
    parser.add_argument("--interval-minutes", type=int, default=None,
                         help="Base minutes between checks in --loop mode. Overrides config.json's schedule.interval_minutes (default: 60).")
    parser.add_argument("--jitter-minutes", type=int, default=None,
                         help="Random 0-N minute jitter added on top of the interval each cycle. Overrides config.json's schedule.jitter_minutes (default: 15). Use 0 to disable.")
    parser.add_argument("--log-server-port", type=int, default=None,
                         help="Port for the web-based log viewer. Overrides config.json's log_server.port (default: 8080).")
    parser.add_argument("--no-log-server", action="store_true",
                         help="Disable the web-based log viewer. Overrides config.json's log_server.enabled.")
    args = parser.parse_args()

    if args.simulate:
        simulate_diff()
        return

    setup_logging()

    try:
        config = load_config()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        print("Please create a config.json file with your USCIS credentials.")
        return

    log_server_enabled, log_server_port = get_log_server_settings(config, args)
    if log_server_enabled:
        start_log_server(LOG_FILE, log_server_port)

    loop, interval_minutes, jitter_minutes = get_schedule_settings(config, args)

    if not loop:
        total_changes, watcher = run_once(args)
        if watcher:
            watcher.close()
        return

    print(f"Running in loop mode - checking every {interval_minutes} minute(s) "
          f"(+0-{jitter_minutes}m jitter). Press Ctrl+C or send SIGTERM to stop.")
    stopper = GracefulStop()

    active_watcher = None

    try:
        while not stopper.stop_requested:
            try:
                total_changes, active_watcher = run_once(args, watcher=active_watcher)
            except Exception as e:
                print(f"Unexpected error during check: {e}")

            if stopper.stop_requested:
                break

            try:
                cycle_config = load_config()
            except Exception:
                cycle_config = config
            _, interval_minutes, jitter_minutes = get_schedule_settings(cycle_config, args)

            jitter_seconds = random.randint(0, jitter_minutes * 60) if jitter_minutes > 0 else 0
            wait_seconds = interval_minutes * 60 + jitter_seconds
            wait_minutes = wait_seconds / 60
            print(f"\nSleeping {wait_minutes:.1f} minute(s) until next check "
                  f"({interval_minutes}m base + {jitter_seconds // 60}m{jitter_seconds % 60:02d}s jitter)...")
            stopper.sleep(wait_seconds)
    finally:
        if active_watcher:
            active_watcher.close()

    print("Stopped.")


if __name__ == "__main__":
    main()