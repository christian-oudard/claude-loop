"""End-to-end tests for claude-loop using live claude --model haiku instances.

Tests the full loop by sending /loop commands via stream-json mode and
verifying hook behavior through a logging wrapper.

Modeled after the manual smoke tests:
  /loop 3 Count upward, one number per iteration.
  /loop 10 Count upward. Stop after five.

Requires: claude CLI, valid API credentials, network access.
Run with:  pytest tests/test_e2e.py -v -s
Skip with: SKIP_E2E=1 pytest
"""

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent

pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_E2E") or shutil.which("claude") is None,
    reason="E2E tests require claude CLI (set SKIP_E2E=1 to skip)",
)


class ClaudeRunner:
    """Runs claude in stream-json mode with hooks configured in an isolated project."""

    def __init__(self, tmp_path, *, stop_after_hook=None):
        """
        Args:
            stop_after_hook: If set, delete loop.json after this many hook
                calls. Simulates `claude-loop stop` / pressing Escape and
                running /loop stop mid-loop.
        """
        self.project_dir = tmp_path / "project"
        self.project_dir.mkdir()
        dot_claude = self.project_dir / ".claude"
        dot_claude.mkdir()

        # Copy /loop slash command into test project
        commands_dst = dot_claude / "commands"
        commands_dst.mkdir()
        shutil.copy(PROJECT_ROOT / "commands" / "loop.md", commands_dst / "loop.md")

        self.hook_log = tmp_path / "hook_calls.jsonl"
        self.loop_json = dot_claude / "loop.json"
        self._setup_hook(tmp_path, stop_after_hook)

    def _setup_hook(self, tmp_path, stop_after_hook):
        hook_wrapper = tmp_path / "hook_wrapper.sh"
        self.settings_file = tmp_path / "settings.json"

        # The hook wrapper logs events, optionally runs `claude-loop stop`
        # after N calls, then delegates to claude-loop hook.
        stop_logic = ""
        if stop_after_hook is not None:
            stop_logic = f"""\
COUNT=$(wc -l < {self.hook_log})
if [ "$COUNT" -ge {stop_after_hook} ]; then
    cd {self.project_dir}
    python3 {PROJECT_ROOT}/claude_loop.py stop
fi
"""

        hook_wrapper.write_text(f"""\
#!/bin/bash
EVENT=$(cat)
echo "$EVENT" >> {self.hook_log}
{stop_logic}cd {self.project_dir}
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

    def loop(self, n, task, *, timeout=120):
        """Send /loop N TASK and wait for completion.

        Returns (messages, hook_calls, timed_out).
        """
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        proc = subprocess.Popen(
            [
                "claude", "--model", "haiku", "-p",
                "--input-format", "stream-json",
                "--output-format", "stream-json", "--verbose",
                "--dangerously-skip-permissions",
                "--setting-sources", "project,local",
                "--settings", str(self.settings_file),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(self.project_dir),
            env=env,
        )

        output_lines = []
        def reader():
            for line in proc.stdout:
                line = line.strip()
                if line:
                    output_lines.append(line)

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": f"/loop {n} {task}"},
        })
        proc.stdin.write(msg + "\n")
        proc.stdin.flush()
        proc.stdin.close()

        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            proc.wait()

        t.join(timeout=5)

        messages = self._parse_messages(output_lines)
        hook_calls = self._parse_hook_log()
        return messages, hook_calls, timed_out

    def _parse_messages(self, lines):
        msgs = []
        for line in lines:
            try:
                m = json.loads(line)
                if m.get("type") == "assistant":
                    for c in m.get("message", {}).get("content", []):
                        if c.get("type") == "text":
                            msgs.append(c["text"])
            except json.JSONDecodeError:
                pass
        return msgs

    def _parse_hook_log(self):
        if not self.hook_log.exists():
            return []
        events = []
        for line in self.hook_log.read_text().strip().split("\n"):
            if line.strip():
                events.append(json.loads(line))
        return events

    def loop_file_exists(self):
        return self.loop_json.exists()


class TestIterationExhaustion:
    """Test: /loop 3 <counting task>

    The loop should run for exactly 3 iterations and terminate when
    iterations are exhausted.
    """

    TASK = (
        "We're testing the loop. Say one number per loop, counting upward. "
        "Use number words, not digits."
    )

    def test_runs_three_iterations(self, tmp_path):
        runner = ClaudeRunner(tmp_path)
        messages, hooks, timed_out = runner.loop(3, self.TASK)

        assert not timed_out, "Loop should complete within timeout"
        # 3 work iterations = at least 3 hook calls (plus the exhaustion end)
        assert len(hooks) >= 3, (
            f"Expected 3+ hook calls for 3 iterations, got {len(hooks)}. "
            f"Messages: {[h.get('last_assistant_message', '')[:40] for h in hooks]}"
        )
        assert not runner.loop_file_exists(), \
            "Loop file should be deleted when iterations exhausted"

    def test_hook_receives_stop_events(self, tmp_path):
        runner = ClaudeRunner(tmp_path)
        _, hooks, _ = runner.loop(2, self.TASK)

        for hook in hooks:
            assert hook["hook_event_name"] == "Stop"
            assert "transcript_path" in hook
            assert len(hook.get("last_assistant_message", "")) > 0


class TestEarlyCompletion:
    """Test: /loop 10 <task that ends after five>

    The agent should count to five, say TASK_COMPLETE, pass verification,
    and end the loop well before 10 iterations.
    """

    TASK = (
        "We're testing the loop. Say one number per loop, counting upward. "
        "Use number words, not digits. Stop counting after the number five."
    )

    def test_completes_before_iteration_limit(self, tmp_path):
        runner = ClaudeRunner(tmp_path)
        messages, hooks, timed_out = runner.loop(10, self.TASK, timeout=180)

        assert not timed_out, "Loop should complete within timeout"
        hook_msgs = [h.get("last_assistant_message", "") for h in hooks]

        # Loop should end in fewer than 10 hook calls
        assert len(hooks) < 10, (
            f"Expected early completion, got {len(hooks)} hooks. "
            f"Messages: {[m[:40] for m in hook_msgs]}"
        )
        assert not runner.loop_file_exists(), \
            "Loop file should be deleted after completion"

    def test_task_complete_detected(self, tmp_path):
        runner = ClaudeRunner(tmp_path)
        _, hooks, _ = runner.loop(10, self.TASK, timeout=180)

        hook_msgs = [h.get("last_assistant_message", "") for h in hooks]
        saw_complete = any("TASK_COMPLETE" in m for m in hook_msgs)

        assert saw_complete, (
            "Agent should have said TASK_COMPLETE after counting to five. "
            f"Messages: {[m[:60] for m in hook_msgs]}"
        )


class TestLoopStop:
    """Test: /loop stop (simulated by deleting loop.json mid-loop).

    Start a 10-iteration loop, delete loop.json after 2 hook calls,
    and verify the loop terminates early without exhausting iterations.
    """

    TASK = (
        "We're testing the loop. Say one number per loop, counting upward. "
        "Use number words, not digits."
    )

    def test_stop_terminates_loop(self, tmp_path):
        runner = ClaudeRunner(tmp_path, stop_after_hook=2)
        messages, hooks, timed_out = runner.loop(10, self.TASK, timeout=120)

        assert not timed_out, "Loop should terminate after stop"
        # Should have roughly 2-3 hook calls: the first 2 fire normally,
        # then on the 3rd the loop.json is gone so the hook is a no-op
        # and claude exits.
        assert len(hooks) < 10, (
            f"Expected early stop, got {len(hooks)} hooks. "
            f"Messages: {[h.get('last_assistant_message', '')[:40] for h in hooks]}"
        )
        assert not runner.loop_file_exists(), \
            "Loop file should be deleted by stop"
