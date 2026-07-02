"""Shared test helpers for persist."""

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


def make_project(tmp_path):
    """Create a minimal project directory with .claude/."""
    dot_claude = tmp_path / ".claude"
    dot_claude.mkdir()
    return tmp_path, dot_claude


def read_session(dot_claude):
    """Read the persist.json state, or None if no session."""
    state_file = dot_claude / "persist.json"
    if state_file.exists():
        return json.loads(state_file.read_text())
    return None


def write_session(dot_claude, iteration, prompt, total=None,
                  deadline=None, started=None, lock=False):
    """Write the active session state to persist.json."""
    state_file = dot_claude / "persist.json"
    entry = {
        "iteration": iteration,
        "prompt": prompt,
        "total": total,
        "deadline": deadline,
        "started": started,
    }
    if lock:
        entry["lock"] = True
    state_file.write_text(json.dumps(entry))


def run_persist(cwd, func, stdin_text):
    """Run a persist function as a subprocess with piped stdin."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-c", f"import persist; persist.{func}()"],
        input=stdin_text,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
    )


def run_main(cwd, args, stdin_text=""):
    """Run persist.main() as a subprocess with given argv and stdin."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    argv = ["persist"] + args
    code = f"import sys; sys.argv = {argv!r}; import persist; persist.main()"
    return subprocess.run(
        [sys.executable, "-c", code],
        input=stdin_text, capture_output=True, text=True,
        cwd=str(cwd), env=env,
    )


def run_start(cwd, stdin_text):
    return run_persist(cwd, "start", stdin_text)


def run_status(cwd):
    return run_persist(cwd, "status", "")


def run_status_short(cwd):
    return run_main(cwd, ["status", "--short"])


def run_active(cwd):
    return run_persist(cwd, "active", "")


def run_hook(cwd, event):
    """Run persist hook and return parsed JSON output (or None)."""
    result = run_persist(cwd, "hook", json.dumps(event))
    if result.returncode != 0 and result.stderr:
        raise RuntimeError(f"Hook failed: {result.stderr}")
    stdout = result.stdout.strip()
    if stdout:
        return json.loads(stdout)
    return None


def make_stop_event(last_msg=""):
    return {
        "hook_event_name": "Stop",
        "last_assistant_message": last_msg,
    }


def make_pre_tool_use_event(tool_name, tool_input):
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }
