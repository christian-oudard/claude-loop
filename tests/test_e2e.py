"""End-to-end tests for claude-loop using PTY-driven claude --model haiku.

Spawns real Claude Code instances in pseudo-terminals, types commands,
sends Escape to interrupt, and verifies hook behavior via log files.

Requires: claude CLI, valid API credentials, network access.
Run with:  pytest tests/test_e2e.py -v -s
Skip with: SKIP_E2E=1 pytest
"""

import json
import os
import pty
import re
import select
import signal
import shutil
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
ESC = b'\x1b'

pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_E2E") or shutil.which("claude") is None,
    reason="E2E tests require claude CLI (set SKIP_E2E=1 to skip)",
)


# --- PTY helpers ---

def pty_read_until(fd, pattern, timeout=60):
    """Read from PTY fd until pattern found or timeout. Returns (data, found)."""
    buf = b''
    deadline = time.monotonic() + timeout
    if isinstance(pattern, str):
        pattern = pattern.encode()
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        r, _, _ = select.select([fd], [], [], min(remaining, 0.1))
        if r:
            try:
                data = os.read(fd, 4096)
                if not data:
                    break
                buf += data
                if pattern in buf:
                    return buf, True
            except OSError:
                break
    return buf, False


def pty_drain(fd, timeout=2.0):
    """Read all available data from PTY."""
    buf = b''
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r, _, _ = select.select([fd], [], [], min(deadline - time.monotonic(), 0.1))
        if r:
            try:
                data = os.read(fd, 4096)
                if not data:
                    break
                buf += data
            except OSError:
                break
    return buf


def strip_ansi(text):
    """Remove ANSI escape sequences from terminal output."""
    text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    text = re.sub(r'\x1b\][^\x07]*\x07', '', text)
    return text


# Input prompt marker: Claude Code shows "shift+tab" hint when ready for input.
INPUT_READY = b"shift+tab"


# --- Test infrastructure ---

class ClaudePTY:
    """Drives a Claude Code instance through a pseudo-terminal."""

    def __init__(self, tmp_path):
        self.project_dir = tmp_path / "project"
        self.project_dir.mkdir()
        dot_claude = self.project_dir / ".claude"
        dot_claude.mkdir()

        commands_dst = dot_claude / "commands"
        commands_dst.mkdir()
        shutil.copy(PROJECT_ROOT / "commands" / "loop.md", commands_dst / "loop.md")

        self.hook_log = tmp_path / "hook_calls.jsonl"
        self.loop_json = dot_claude / "loop.json"
        self._setup_hook(tmp_path)

        self.pid = None
        self.fd = None

    def _setup_hook(self, tmp_path):
        hook_wrapper = tmp_path / "hook_wrapper.sh"
        self.settings_file = tmp_path / "settings.json"

        hook_wrapper.write_text(f"""\
#!/bin/bash
EVENT=$(cat)
echo "$EVENT" >> {self.hook_log}
cd {self.project_dir}
echo "$EVENT" | python3 {PROJECT_ROOT}/claude_loop.py hook
""")
        hook_wrapper.chmod(0o755)

        self.settings_file.write_text(json.dumps({
            "hooks": {
                "Stop": [{
                    "matcher": "",
                    "hooks": [{"type": "command", "command": str(hook_wrapper)}],
                }],
            },
        }))

    def spawn(self):
        """Spawn claude in a PTY. Must call cleanup() when done."""
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env["TERM"] = "xterm-256color"
        env["COLUMNS"] = "120"
        env["LINES"] = "40"

        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(str(self.project_dir))
            os.execvpe("claude", [
                "claude", "--model", "haiku",
                "--dangerously-skip-permissions",
                "--setting-sources", "project,local",
                "--settings", str(self.settings_file),
            ], env)

        self.pid = pid
        self.fd = fd

        # Handle the folder trust dialog
        _, found = pty_read_until(fd, b"trust", timeout=20)
        assert found, "Claude failed to show trust dialog"
        os.write(fd, b"\r")  # Press Enter to confirm trust

        # Wait for input prompt
        _, found = pty_read_until(fd, INPUT_READY, timeout=20)
        assert found, "Claude failed to reach input prompt"
        pty_drain(fd, timeout=1)

    def submit(self, text):
        """Type text and press Enter. Waits for TUI to register the input."""
        if isinstance(text, str):
            text = text.encode()
        os.write(self.fd, text)
        # Wait for the TUI to echo back at least the first few characters,
        # confirming it registered the input before we press Enter.
        prefix = text[:8]
        pty_read_until(self.fd, prefix, timeout=5)
        os.write(self.fd, b"\r")

    def send_escape(self):
        """Send Escape key to interrupt current turn."""
        os.write(self.fd, ESC)

    def wait_for_input_ready(self, timeout=30):
        """Wait for the input prompt to appear (TUI ready for commands)."""
        buf, found = pty_read_until(self.fd, INPUT_READY, timeout=timeout)
        pty_drain(self.fd, timeout=0.5)
        return found

    def wait_for(self, pattern, timeout=90):
        """Wait for pattern to appear in terminal output."""
        buf, found = pty_read_until(self.fd, pattern, timeout=timeout)
        return strip_ansi(buf.decode("utf-8", errors="replace")), found

    def drain(self, timeout=3.0):
        """Drain remaining output."""
        return strip_ansi(pty_drain(self.fd, timeout).decode("utf-8", errors="replace"))

    def wait_for_hook_calls(self, n, timeout=120):
        """Wait until at least n hook calls have been logged."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.count_hook_calls() >= n:
                return True
            # Drain output to keep the PTY buffer from filling up
            pty_drain(self.fd, timeout=0.5)
        return False

    def wait_for_loop_end(self, timeout=180):
        """Wait until loop.json is deleted (loop finished)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pty_drain(self.fd, timeout=0.5)
            if not self.loop_file_exists() and self.count_hook_calls() >= 1:
                return True
        return False

    def wait_for_exit(self, timeout=30):
        """Wait for the claude process to exit."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pty_drain(self.fd, timeout=0.5)
            result = os.waitpid(self.pid, os.WNOHANG)
            if result[0] != 0:
                self.pid = None
                return True
        return False

    def count_hook_calls(self):
        if not self.hook_log.exists():
            return 0
        text = self.hook_log.read_text().strip()
        if not text:
            return 0
        return sum(1 for line in text.split("\n") if line.strip())

    def parse_hook_log(self):
        if not self.hook_log.exists():
            return []
        events = []
        for line in self.hook_log.read_text().strip().split("\n"):
            if line.strip():
                events.append(json.loads(line))
        return events

    def loop_file_exists(self):
        return self.loop_json.exists()

    def cleanup(self):
        """Kill the claude process."""
        if self.pid is not None:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(self.pid, 0)
            except ChildProcessError:
                pass
            self.pid = None


E2E_TIMEOUT = 120  # seconds per test


@pytest.fixture
def claude(tmp_path):
    """Fixture that provides a started ClaudePTY instance with a timeout."""
    prev = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(
        TimeoutError(f"E2E test exceeded {E2E_TIMEOUT}s timeout")))
    signal.alarm(E2E_TIMEOUT)

    c = ClaudePTY(tmp_path)
    c.spawn()
    yield c

    signal.alarm(0)
    signal.signal(signal.SIGALRM, prev)
    # Try graceful exit
    try:
        c.send_escape()
        c.wait_for_input_ready(timeout=10)
        c.submit("/exit")
        c.wait_for_exit(timeout=5)
    except OSError:
        pass
    c.cleanup()


# --- Test tasks ---
# Designed for haiku: extremely explicit, no ambiguity, no state tracking needed.

COUNTING_TASK = (
    "Say one number per loop, counting upward. "
    "Use number words, not digits. This task is never complete."
)

STOP_AT_FIVE_TASK = (
    "Say one number per loop, counting upward. Use number words, not digits. "
    "The task is complete once you have said the number five."
)


# --- Tests ---

class TestIterationExhaustion:
    """Test: /loop 3 <counting task>

    The loop should run for 3 iterations and terminate when exhausted.
    """

    def test_runs_three_iterations(self, claude):
        claude.submit(f"/loop 3 {COUNTING_TASK}")

        got_enough = claude.wait_for_hook_calls(3, timeout=120)
        hooks = claude.parse_hook_log()

        assert got_enough, (
            f"Expected 3+ hook calls, got {len(hooks)}. "
            f"Messages: {[h.get('last_assistant_message', '')[:40] for h in hooks]}"
        )

        # Wait for the loop to clean up
        claude.wait_for_loop_end(timeout=30)

        assert not claude.loop_file_exists(), \
            "Loop file should be deleted when iterations exhausted"

    def test_hook_receives_stop_events(self, claude):
        claude.submit(f"/loop 2 {COUNTING_TASK}")

        claude.wait_for_hook_calls(2, timeout=120)
        claude.wait_for_loop_end(timeout=30)

        hooks = claude.parse_hook_log()
        for hook in hooks:
            assert hook["hook_event_name"] == "Stop"
            assert "transcript_path" in hook
            assert len(hook.get("last_assistant_message", "")) > 0


class TestEarlyCompletion:
    """Test: /loop 10 <task that ends after five>

    The agent should count to five, say TASK_COMPLETE, pass verification,
    and end the loop well before 10 iterations.
    """

    def test_completes_before_iteration_limit(self, claude):
        claude.submit(f"/loop 10 {STOP_AT_FIVE_TASK}")

        claude.wait_for_loop_end(timeout=180)

        hooks = claude.parse_hook_log()
        hook_msgs = [h.get("last_assistant_message", "") for h in hooks]

        assert len(hooks) < 10, (
            f"Expected early completion, got {len(hooks)} hooks. "
            f"Messages: {[m[:40] for m in hook_msgs]}"
        )
        assert not claude.loop_file_exists(), \
            "Loop file should be deleted after completion"

    def test_task_complete_detected(self, claude):
        claude.submit(f"/loop 10 {STOP_AT_FIVE_TASK}")

        claude.wait_for_loop_end(timeout=180)

        hooks = claude.parse_hook_log()
        hook_msgs = [h.get("last_assistant_message", "") for h in hooks]
        saw_complete = any("TASK_COMPLETE" in m for m in hook_msgs)

        assert saw_complete, (
            "Agent should have said TASK_COMPLETE after counting to five. "
            f"Messages: {[m[:60] for m in hook_msgs]}"
        )


class TestLoopStop:
    """Test: /loop stop sent mid-loop via Escape + command.

    Start a 10-iteration loop, wait for 2+ iterations, send Escape to
    interrupt the current turn, then type /loop stop. Verifies the full
    stop path through the real TUI.
    """

    def test_stop_terminates_loop(self, claude):
        claude.submit(f"/loop 10 {COUNTING_TASK}")

        # Wait for at least 2 hook calls (2 iterations done)
        got_enough = claude.wait_for_hook_calls(2, timeout=120)
        assert got_enough, (
            f"Expected 2+ iterations before stopping, got {claude.count_hook_calls()}"
        )

        # Send Escape and wait for input prompt (no sleep!)
        claude.send_escape()
        ready = claude.wait_for_input_ready(timeout=15)
        assert ready, "Prompt did not reappear after Escape"

        # Submit /loop stop
        claude.submit("/loop stop")

        # Wait for loop file to be deleted
        claude.wait_for_loop_end(timeout=30)

        hooks = claude.parse_hook_log()
        assert len(hooks) < 10, (
            f"Expected early stop, got {len(hooks)} hooks. "
            f"Messages: {[h.get('last_assistant_message', '')[:40] for h in hooks]}"
        )
        assert not claude.loop_file_exists(), \
            "Loop file should be deleted by /loop stop"
