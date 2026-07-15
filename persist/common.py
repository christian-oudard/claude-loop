"""Shared utilities for persist."""

import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


def dot_claude_dir(create=False):
    """Find the nearest .claude/ directory below $HOME.

    With create=True, fall back to creating one at the .git project root.
    Read paths must not create, so that queries like `persist status` have
    no side effects.
    """
    project_root = None
    for p in [Path.cwd(), *Path.cwd().parents]:
        if p == Path.home():
            break
        dot_claude = p / '.claude'
        if dot_claude.is_dir():
            return dot_claude
        if project_root is None and (p / '.git').exists():
            project_root = p
    if create and project_root:
        dot_claude = project_root / '.claude'
        dot_claude.mkdir()
        print(f"Found .git in {project_root}, created {dot_claude}/", file=sys.stderr)
        return dot_claude
    return None


def parse_limit(s, now=None):
    """Parse a limit string into (total_iterations, deadline_timestamp).

    Returns a tuple where at most one value is non-None.
    Both are None for forever (no limit).
    Raises ValueError for unparseable input.

    `now` anchors relative deadlines; the caller passes the same value it
    records as `started` so that `deadline - started` equals the nominal
    duration exactly, not a hair less from two separate clock reads.
    """
    if now is None:
        now = time.time()
    s = s.strip()

    # Forever: "forever"
    if s.lower() == 'forever':
        return None, None

    # Duration: "2h", "30m"
    m = re.match(r'^(\d+)h$', s, re.IGNORECASE)
    if m:
        return None, now + int(m.group(1)) * 3600

    m = re.match(r'^(\d+)m$', s, re.IGNORECASE)
    if m:
        return None, now + int(m.group(1)) * 60

    # Clock time: "2pm", "11am"
    m = re.match(r'^(\d{1,2})(am|pm)$', s, re.IGNORECASE)
    if m:
        hour = int(m.group(1))
        if m.group(2).lower() == 'pm' and hour != 12:
            hour += 12
        elif m.group(2).lower() == 'am' and hour == 12:
            hour = 0
        return None, _next_occurrence(hour, 0, now)

    # HH:MM format
    m = re.match(r'^(\d{1,2}):(\d{2})$', s)
    if m:
        return None, _next_occurrence(int(m.group(1)), int(m.group(2)), now)

    # Pure number
    n = int(s)
    if n >= 1000:
        # Military time: 1400 -> 14:00
        hour, minute = divmod(n, 100)
        if hour > 23 or minute > 59:
            raise ValueError(f"Invalid military time: {s}")
        return None, _next_occurrence(hour, minute, now)
    if n < 1:
        raise ValueError(f"Iteration count must be >= 1: {s}")
    return n, None


def _next_occurrence(hour, minute, now=None):
    """Return Unix timestamp for the next occurrence of HH:MM."""
    now = datetime.fromtimestamp(now) if now is not None else datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


def is_expired(data):
    """Check if a session's limit has been reached."""
    deadline = data.get('deadline')
    if deadline and time.time() >= deadline:
        return 'deadline'
    total = data.get('total')
    if total and data.get('iteration', 0) > total:
        return 'iterations'
    return None


def _format_duration(seconds):
    """Format a duration in seconds as e.g. '2h30m' or '45m'."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours > 0:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def format_remaining(data):
    """Format the remaining limit for status display."""
    iteration = data.get('iteration', '?')
    deadline = data.get('deadline')
    if deadline:
        now = time.time()
        started = data.get('started')
        remaining = deadline - now
        if remaining <= 0:
            dt = datetime.fromtimestamp(deadline)
            clock = dt.strftime('%H:%M')
            return f"{iteration}, until {clock} (expired)"
        if started:
            elapsed = now - started
            total_duration = deadline - started
            return f"{iteration}, {_format_duration(elapsed)}/{_format_duration(total_duration)}"
        return f"{iteration}, {_format_duration(remaining)} remaining"
    total = data.get('total')
    if total:
        return f"{iteration}/{total}"
    started = data.get('started')
    if started:
        elapsed = time.time() - started
        return f"{iteration}, {_format_duration(elapsed)} (forever)"
    return f"{iteration} (forever)"
