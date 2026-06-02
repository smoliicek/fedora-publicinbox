#!/usr/bin/env python3
"""
backfill.py — historical import of Fedora git.receive messages into public-inbox

Pulls org.fedoraproject.prod.git.receive messages from Fedora's public
datagrepper API and injects them via public-inbox-mda, reusing the same
_build_email / _inject logic as the live consumer.

Usage:
    python3 backfill.py [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--dry-run]

Defaults:
    --start  2026-01-01
    --end    today

The script saves a cursor file (backfill_cursor.json) next to itself so it
can be safely interrupted and resumed.  Because Message-IDs are deterministic
(repo + rev), re-injecting an already-present message is a no-op for
public-inbox-mda.

Environment variables (override defaults):
    PUBLIC_INBOX_DIR    path to the public-inbox data dir  (default: /var/lib/public-inbox)
    LIST_ADDRESS        list address                        (default: git-commits@fedoraproject.org)
    PI_CONFIG           path to public-inbox config file   (default: $PUBLIC_INBOX_DIR/config)
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import format_datetime, formatdate
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

DATAGREPPER_URL = "https://apps.fedoraproject.org/datagrepper/v2/search"
TOPIC           = "org.fedoraproject.prod.git.receive"
ROWS_PER_PAGE   = 50       # datagrepper max is 100; 50 is polite
RETRY_WAIT      = 10       # seconds between retries on transient errors
MAX_RETRIES     = 5
INJECT_DELAY    = 0.05     # seconds between mda calls — be gentle on disk I/O

CURSOR_FILE     = Path(__file__).parent / "backfill_cursor.json"

log = logging.getLogger("backfill")

# yes, i just ctrl+c and ctrl+v from consumer.py, so what?
def _build_email(commit: dict, list_address: str, sent_at: str = "") -> str:
    repo      = commit.get("repo", "unknown")
    namespace = commit.get("namespace", "")
    branch    = commit.get("branch", "unknown")
    rev       = commit.get("rev", "")
    summary   = commit.get("summary", "(no summary)")
    msg_body  = commit.get("message", "")
    patch     = commit.get("patch", "")
    url       = commit.get("url", "")
    author    = commit.get("name", "Unknown")
    email_    = commit.get("email", list_address)
    date      = commit.get("date", "")

    full_repo = f"{namespace}/{repo}" if namespace else repo
    subject   = f"[{full_repo}] {branch}: {summary}"

    stats = commit.get("stats", {}).get("total", {})
    stats_line = (
        f"+{stats.get('additions', 0)}/-{stats.get('deletions', 0)} "
        f"in {stats.get('files', 0)} file(s)"
    )

    body_text = textwrap.dedent(f"""\
        A new commit has been pushed.

        Repo   : {full_repo}
        Branch : {branch}
        Commit : {rev}
        Author : {author} <{email_}>
        Date   : {date}
        Stats  : {stats_line}
        URL    : {url}

        Log:
        {msg_body.strip()}
    """)

    if patch:
        body_text += f"\n---\n{patch}"

    msg = MIMEText(body_text, "plain", "utf-8")
    msg["From"]       = f"{author} <{email_}>"
    msg["To"]         = list_address
    msg["Subject"]    = subject
    if sent_at:
        try:
            msg["Date"] = format_datetime(datetime.fromisoformat(sent_at))
        except ValueError:
            msg["Date"] = formatdate(usegmt=True)
    else:
        msg["Date"] = formatdate(usegmt=True)
    msg["Message-ID"] = f"<{full_repo.replace('/', '-')}-{rev[:12]}@fedoraproject.org>"
    msg["List-ID"]     = f"<{list_address.replace('@', '.')}>"
    msg["X-Git-Repo"]  = full_repo
    msg["X-Git-Branch"] = branch
    msg["X-Git-Rev"]   = rev

    return msg.as_string()


def _inject(email_str: str, public_inbox_dir: str, list_address: str) -> None:
    env = os.environ.copy()
    env["ORIGINAL_RECIPIENT"] = list_address
    env["HOME"]               = public_inbox_dir
    subprocess.run(
        ["public-inbox-mda"],
        input=email_str,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

def _fetch_page(start_ts: int, end_ts: int, page: int) -> dict:
    params = urlencode({
        "topic":         TOPIC,
        "start":         start_ts,
        "end":           end_ts,
        "rows_per_page": ROWS_PER_PAGE,
        "page":          page,
        "order":         "asc",   # oldest first
    })
    url = f"{DATAGREPPER_URL}?{params}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urlopen(url, timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            if e.code == 429:
                wait = RETRY_WAIT * attempt
                log.warning("Rate-limited (429); waiting %ds …", wait)
                time.sleep(wait)
            else:
                raise
        except URLError as e:
            log.warning("Network error (attempt %d/%d): %s", attempt, MAX_RETRIES, e)
            time.sleep(RETRY_WAIT)

    raise RuntimeError(f"Failed to fetch page {page} after {MAX_RETRIES} attempts")


def iter_messages(start_ts: int, end_ts: int, resume_page: int = 1):
    """Yield (page, raw_message_body_dict) for every git.receive in range."""
    page = resume_page
    while True:
        log.info("Fetching page %d …", page)
        data = _fetch_page(start_ts, end_ts, page)

        messages = data.get("raw_messages", [])
        if not messages:
            log.info("No more messages on page %d — done.", page)
            break

        for msg in messages:
            body    = msg.get("body", {})
            sent_at = msg.get("headers", {}).get("sent-at", "")
            yield page, body, sent_at

        total_pages = data.get("pages", 1)
        if page >= total_pages:
            break

        page += 1
        time.sleep(0.2)   # be polite to datagrepper

def load_cursor() -> dict:
    if CURSOR_FILE.exists():
        return json.loads(CURSOR_FILE.read_text())
    return {}


def save_cursor(page: int, injected: int) -> None:
    CURSOR_FILE.write_text(json.dumps({"page": page, "injected": injected}))

def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start",   default="2026-01-01", help="Start date YYYY-MM-DD (default: 2026-01-01)")
    p.add_argument("--end",     default=None,         help="End date YYYY-MM-DD   (default: today)")
    p.add_argument("--dry-run", action="store_true",  help="Build emails but do not call public-inbox-mda")
    p.add_argument("--reset",   action="store_true",  help="Ignore saved cursor and start from --start")
    p.add_argument("--filter-by-commit-date", action="store_true",
                   help="Skip commits whose actual commit date falls outside --start/--end (filters Mass Rebuild replays etc.)")
    return p.parse_args()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
        stream=sys.stdout,
    )

    args = parse_args()

    public_inbox_dir = os.environ.get("PUBLIC_INBOX_DIR", "/var/lib/public-inbox")
    list_address     = os.environ.get("LIST_ADDRESS",     "git-commits@fedoraproject.org")

    start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt   = (
        datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if args.end
        else datetime.now(timezone.utc)
    )

    start_ts = int(start_dt.timestamp())
    end_ts   = int(end_dt.timestamp())

    log.info("Backfill range : %s → %s", start_dt.date(), end_dt.date())
    log.info("public-inbox   : %s", public_inbox_dir)
    log.info("list address   : %s", list_address)
    log.info("dry-run        : %s", args.dry_run)
    log.info("filter-by-date : %s", args.filter_by_commit_date)

    cursor   = {} if args.reset else load_cursor()
    resume_page = cursor.get("page", 1)
    injected    = cursor.get("injected", 0)

    if cursor and not args.reset:
        log.info("Resuming from page %d (%d already injected)", resume_page, injected)

    skipped  = 0
    errors   = 0
    last_page = resume_page

    try:
        for page, body, sent_at in iter_messages(start_ts, end_ts, resume_page):
            commit = body.get("commit", {})
            rev    = commit.get("rev", "")

            if not rev:
                log.debug("Skipping message with no rev")
                skipped += 1
                continue

            if args.filter_by_commit_date:
                raw_date = commit.get("date", "")
                if raw_date:
                    try:
                        commit_dt = datetime.fromisoformat(raw_date).replace(tzinfo=timezone.utc)
                        if commit_dt < start_dt or commit_dt > end_dt:
                            log.debug("Skipping commit outside date range: %s @ %s", rev[:12], raw_date)
                            skipped += 1
                            continue
                    except ValueError:
                        pass  # unparseable date, let it through

            try:
                email_str = _build_email(commit, list_address, sent_at)
            except Exception as e:
                log.warning("Could not build email for rev %s: %s", rev[:12], e)
                errors += 1
                continue

            if args.dry_run:
                commit_date = commit.get("date", "unknown")
                full_repo = f"{commit.get('namespace', '')}/{commit.get('repo', '?')}".lstrip("/")
                log.info("[dry-run] Would inject: %s @ %s (sent-at: %s, commit date: %s)", full_repo, rev[:12], sent_at, commit_date)
            else:
                try:
                    _inject(email_str, public_inbox_dir, list_address)
                    injected += 1
                    if injected % 100 == 0:
                        log.info("Progress: %d injected, %d errors, %d skipped", injected, errors, skipped)
                except subprocess.CalledProcessError as e:
                    log.error("mda failed for rev %s (exit %d): %s", rev[:12], e.returncode, e.stderr)
                    errors += 1

            if page != last_page:
                save_cursor(page, injected)
                last_page = page

            time.sleep(INJECT_DELAY)

    except KeyboardInterrupt:
        log.info("Interrupted — cursor saved at page %d", last_page)
        save_cursor(last_page, injected)
        sys.exit(1)

    # re-index, else it won't show up
    if not args.dry_run and injected > 0:
        log.info("Running public-inbox-index …")
        env = os.environ.copy()
        env["HOME"] = public_inbox_dir
        subprocess.run(
            ["public-inbox-index", public_inbox_dir],
            env=env,
            check=True,
        )

    log.info("Done. injected=%d errors=%d skipped=%d", injected, errors, skipped)

    # clean up after yourself kids!
    if CURSOR_FILE.exists() and not args.dry_run:
        CURSOR_FILE.unlink()


if __name__ == "__main__":
    main()
