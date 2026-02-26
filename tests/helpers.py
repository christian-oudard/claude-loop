"""Shared test helpers for claude_loop."""

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


def read_loop_file(dot_claude):
    loop_file = dot_claude / "loop.json"
    if loop_file.exists():
        return json.loads(loop_file.read_text())
    return None


def write_loop_file(dot_claude, iteration, prompt, total):
    (dot_claude / "loop.json").write_text(json.dumps({
        "iteration": iteration,
        "prompt": prompt,
        "total": total,
    }))


def run_claude_loop(cwd, func, stdin_text):
    """Run a claude_loop function as a subprocess with piped stdin."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", f"import claude_loop; claude_loop.{func}()"],
        input=stdin_text,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
    )
    return result


def run_main(cwd, args, stdin_text=""):
    """Run claude_loop.main() as a subprocess with given argv and stdin."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    argv = ["claude-loop"] + args
    code = f"import sys; sys.argv = {argv!r}; import claude_loop; claude_loop.main()"
    return subprocess.run(
        [sys.executable, "-c", code],
        input=stdin_text, capture_output=True, text=True, cwd=str(cwd), env=env,
    )


def run_start(cwd, stdin_text):
    return run_claude_loop(cwd, "start", stdin_text)


def run_status(cwd):
    return run_claude_loop(cwd, "status", "")


def run_hook(cwd, event):
    """Run claude-loop hook and return parsed JSON output (or None)."""
    result = run_claude_loop(cwd, "hook", json.dumps(event))
    if result.returncode != 0 and result.stderr:
        raise RuntimeError(f"Hook failed: {result.stderr}")
    stdout = result.stdout.strip()
    if stdout:
        return json.loads(stdout)
    return None
