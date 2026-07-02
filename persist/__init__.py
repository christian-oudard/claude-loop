"""Persistent coding sessions for Claude Code.

    persist              Start from /persist:go slash command
    persist hook         Hook handler (Stop + PreToolUse)
    persist stop         Cancel a running session
    persist status       Show session status (always exits 0)
    persist status -s    One-line status for statuslines (--short)
    persist active       Exit 0 if a live session is running, else 1
"""

import json
import re
import sys

from .common import parse_limit, is_expired, format_remaining  # noqa: F401
from .session import (  # noqa: F401
    start, stop, status, active, stop_hook, find_keyword,
    read_session, write_session, delete_session, _state_path,
)


def main():
    args = sys.argv[1:]
    if '-h' in args or '--help' in args:
        print(__doc__.strip())
        return
    cmd = args[0] if args else None
    if cmd is None:
        start()
    elif cmd == 'hook':
        hook()
    elif cmd == 'stop':
        stop()
    elif cmd == 'status':
        status(short=any(a in ('-s', '--short') for a in sys.argv[2:]))
    elif cmd == 'active':
        active()
    else:
        print(f"persist: unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)


def hook():
    event = json.loads(sys.stdin.read())
    event_name = event.get('hook_event_name', '')

    if event_name == 'Stop':
        _stop(event)
    elif event_name == 'PreToolUse':
        _pre_tool_use(event)


def _is_persist_stop_attempt(event):
    """Check if a PreToolUse event is trying to run persist stop."""
    tool_name = event.get('tool_name', '')
    tool_input = event.get('tool_input', {})

    if tool_name == 'Bash':
        cmd = tool_input.get('command', '')
        return bool(re.search(r'\bpersist\s+stop\b', cmd))

    if tool_name == 'Skill':
        skill = tool_input.get('skill', '')
        return skill == 'persist:stop'

    return False


def _pre_tool_use(event):
    if not _is_persist_stop_attempt(event):
        return

    if read_session() is not None:
        print(json.dumps({
            "decision": "block",
            "reason": "Cannot stop your own persist session. "
                      "Ask the user to run /persist:stop manually.",
        }))


def _stop(event):
    state = read_session()
    if state is None:
        return
    stop_hook(state, event)
