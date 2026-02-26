"""Unit and integration tests for claude_loop core logic."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Import from the project
sys.path.insert(0, str(Path(__file__).parent.parent))
import claude_loop


# --- Unit tests: find_keyword ---

class TestFindKeyword:
    def test_task_complete(self):
        assert claude_loop.find_keyword("blah TASK_COMPLETE blah") == "TASK_COMPLETE"

    def test_review_okay(self):
        assert claude_loop.find_keyword("REVIEW_OKAY") == "REVIEW_OKAY"

    def test_review_incomplete(self):
        assert claude_loop.find_keyword("some text REVIEW_INCOMPLETE more") == "REVIEW_INCOMPLETE"

    def test_no_keyword(self):
        assert claude_loop.find_keyword("just normal text") is None

    def test_empty(self):
        assert claude_loop.find_keyword("") is None

    def test_priority_task_complete_first(self):
        # TASK_COMPLETE is checked first due to iteration order
        assert claude_loop.find_keyword("TASK_COMPLETE REVIEW_OKAY") == "TASK_COMPLETE"


# --- Unit tests: reverse_lines ---

class TestReverseLines:
    def test_basic(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("line1\nline2\nline3\n")
        assert list(claude_loop.reverse_lines(str(f))) == ["line3", "line2", "line1"]

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        assert list(claude_loop.reverse_lines(str(f))) == []

    def test_single_line(self, tmp_path):
        f = tmp_path / "single.txt"
        f.write_text("only\n")
        assert list(claude_loop.reverse_lines(str(f))) == ["only"]

    def test_no_trailing_newline(self, tmp_path):
        f = tmp_path / "notail.txt"
        f.write_text("a\nb")
        assert list(claude_loop.reverse_lines(str(f))) == ["b", "a"]

    def test_small_block_size(self, tmp_path):
        """Test with a tiny block size to exercise chunking."""
        f = tmp_path / "chunked.txt"
        f.write_text("alpha\nbeta\ngamma\ndelta\n")
        result = list(claude_loop.reverse_lines(str(f), block_size=8))
        assert result == ["delta", "gamma", "beta", "alpha"]


# --- Unit tests: parse_loop_args ---

class TestParseLoopArgs:
    def _write_transcript(self, tmp_path, messages):
        f = tmp_path / "transcript.jsonl"
        lines = [json.dumps(m) for m in messages]
        f.write_text("\n".join(lines) + "\n")
        return str(f)

    def test_basic_parse(self, tmp_path):
        path = self._write_transcript(tmp_path, [
            {"type": "user", "message": {"content": "<command-name>/loop</command-name><command-args>5 Fix the bug</command-args>"}},
            {"type": "assistant", "message": {"content": "OK"}},
        ])
        result = claude_loop.parse_loop_args(path)
        assert result == (5, "Fix the bug")

    def test_stop_command(self, tmp_path):
        path = self._write_transcript(tmp_path, [
            {"type": "user", "message": {"content": "<command-name>/loop</command-name><command-args>stop</command-args>"}},
        ])
        result = claude_loop.parse_loop_args(path)
        assert result == "stop"

    def test_no_loop_command(self, tmp_path):
        path = self._write_transcript(tmp_path, [
            {"type": "user", "message": {"content": "just a normal message"}},
        ])
        result = claude_loop.parse_loop_args(path)
        assert result is None

    def test_finds_most_recent(self, tmp_path):
        """reverse_lines reads backwards, so the last /loop is found first."""
        path = self._write_transcript(tmp_path, [
            {"type": "user", "message": {"content": "<command-name>/loop</command-name><command-args>3 Old task</command-args>"}},
            {"type": "assistant", "message": {"content": "Working..."}},
            {"type": "user", "message": {"content": "<command-name>/loop</command-name><command-args>5 New task</command-args>"}},
        ])
        result = claude_loop.parse_loop_args(path)
        assert result == (5, "New task")


# --- Integration tests: hook state machine ---

class TestHookStateMachine:
    """Test the hook by calling claude-loop hook as a subprocess with crafted events.

    This tests the full state machine including file I/O, without needing
    a live Claude Code instance.
    """

    def _make_project(self, tmp_path):
        """Create a minimal project directory with .claude/."""
        dot_claude = tmp_path / ".claude"
        dot_claude.mkdir()
        return tmp_path, dot_claude

    def _write_loop_file(self, dot_claude, remaining, prompt, total):
        loop_file = dot_claude / "loop.json"
        loop_file.write_text(json.dumps({
            "remaining": remaining,
            "prompt": prompt,
            "total": total,
        }))

    def _read_loop_file(self, dot_claude):
        loop_file = dot_claude / "loop.json"
        if loop_file.exists():
            return json.loads(loop_file.read_text())
        return None

    def _make_transcript(self, tmp_path, n, task):
        """Create a transcript file with a /loop command."""
        t = tmp_path / "transcript.jsonl"
        t.write_text(json.dumps({
            "type": "user",
            "message": {"content": f"<command-name>/loop</command-name><command-args>{n} {task}</command-args>"},
        }) + "\n")
        return str(t)

    def _run_hook(self, cwd, event):
        """Run claude-loop hook as a subprocess."""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).parent.parent)
        result = subprocess.run(
            [sys.executable, "-c", "import claude_loop; claude_loop.hook()"],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            cwd=str(cwd),
            env=env,
        )
        if result.returncode != 0 and result.stderr:
            raise RuntimeError(f"Hook failed: {result.stderr}")
        stdout = result.stdout.strip()
        if stdout:
            return json.loads(stdout)
        return None

    def test_first_hook_sets_up_loop(self, tmp_path):
        """First hook call parses transcript and sets up loop state."""
        proj, dot_claude = self._make_project(tmp_path)
        self._write_loop_file(dot_claude, 0, None, 0)  # placeholder from start()
        transcript = self._make_transcript(tmp_path, 3, "Write hello world")

        event = {
            "hook_event_name": "Stop",
            "transcript_path": transcript,
            "last_assistant_message": "I'll start working on this.",
        }
        decision = self._run_hook(proj, event)

        assert decision is not None
        assert decision["decision"] == "block"
        assert "Loop iteration" in decision["reason"]
        assert "Write hello world" in decision["reason"]

        state = self._read_loop_file(dot_claude)
        assert state["remaining"] == 2  # 3 - 1
        assert state["prompt"] == "Write hello world"
        assert state["total"] == 3

    def test_normal_continuation(self, tmp_path):
        """Hook decrements remaining and continues."""
        proj, dot_claude = self._make_project(tmp_path)
        self._write_loop_file(dot_claude, 3, "Do the thing", 5)

        event = {
            "hook_event_name": "Stop",
            "transcript_path": "/dev/null",
            "last_assistant_message": "Made some progress.",
        }
        decision = self._run_hook(proj, event)

        assert decision["decision"] == "block"
        assert "Loop iteration" in decision["reason"]
        state = self._read_loop_file(dot_claude)
        assert state["remaining"] == 2

    def test_task_complete_triggers_verification(self, tmp_path):
        """TASK_COMPLETE keyword triggers verification prompt."""
        proj, dot_claude = self._make_project(tmp_path)
        self._write_loop_file(dot_claude, 3, "Build feature X", 5)

        event = {
            "hook_event_name": "Stop",
            "transcript_path": "/dev/null",
            "last_assistant_message": "Done! TASK_COMPLETE",
        }
        decision = self._run_hook(proj, event)

        assert decision["decision"] == "block"
        assert "Verification" in decision["reason"]
        assert "Build feature X" in decision["reason"]
        # Loop file should still exist (verification is an iteration)
        state = self._read_loop_file(dot_claude)
        assert state is not None

    def test_review_okay_ends_loop(self, tmp_path):
        """REVIEW_OKAY ends the loop."""
        proj, dot_claude = self._make_project(tmp_path)
        self._write_loop_file(dot_claude, 2, "Build feature X", 5)

        event = {
            "hook_event_name": "Stop",
            "transcript_path": "/dev/null",
            "last_assistant_message": "Everything looks good. REVIEW_OKAY",
        }
        decision = self._run_hook(proj, event)

        assert decision["decision"] == "block"
        assert "verified" in decision["reason"].lower()
        # Loop file should be deleted
        assert self._read_loop_file(dot_claude) is None

    def test_review_incomplete_continues(self, tmp_path):
        """REVIEW_INCOMPLETE continues the loop with work prompt."""
        proj, dot_claude = self._make_project(tmp_path)
        self._write_loop_file(dot_claude, 2, "Build feature X", 5)

        event = {
            "hook_event_name": "Stop",
            "transcript_path": "/dev/null",
            "last_assistant_message": "Found a bug. REVIEW_INCOMPLETE",
        }
        decision = self._run_hook(proj, event)

        assert decision["decision"] == "block"
        assert "Loop iteration" in decision["reason"]
        state = self._read_loop_file(dot_claude)
        assert state["remaining"] == 1

    def test_iterations_exhausted(self, tmp_path):
        """Loop ends when remaining hits zero."""
        proj, dot_claude = self._make_project(tmp_path)
        self._write_loop_file(dot_claude, 1, "Do stuff", 3)

        event = {
            "hook_event_name": "Stop",
            "transcript_path": "/dev/null",
            "last_assistant_message": "Still working...",
        }
        decision = self._run_hook(proj, event)

        assert decision["decision"] == "block"
        assert "exhausted" in decision["reason"].lower()
        assert self._read_loop_file(dot_claude) is None

    def test_no_loop_file_silent_exit(self, tmp_path):
        """Hook exits silently when no loop file exists."""
        proj, _ = self._make_project(tmp_path)
        # No loop.json written

        event = {
            "hook_event_name": "Stop",
            "transcript_path": "/dev/null",
            "last_assistant_message": "Hello",
        }
        decision = self._run_hook(proj, event)
        assert decision is None

    def test_non_stop_event_ignored(self, tmp_path):
        """Non-Stop events are ignored."""
        proj, dot_claude = self._make_project(tmp_path)
        self._write_loop_file(dot_claude, 3, "task", 5)

        event = {
            "hook_event_name": "NotStop",
            "transcript_path": "/dev/null",
            "last_assistant_message": "Hello",
        }
        decision = self._run_hook(proj, event)
        assert decision is None
        # Loop file should be unchanged
        state = self._read_loop_file(dot_claude)
        assert state["remaining"] == 3

    def test_full_lifecycle(self, tmp_path):
        """Test a complete loop lifecycle: start -> work -> complete -> verify -> done."""
        proj, dot_claude = self._make_project(tmp_path)
        transcript = self._make_transcript(tmp_path, 5, "Create hello.txt")

        # 1. Start: write placeholder
        self._write_loop_file(dot_claude, 0, None, 0)

        # 2. First hook: parse transcript, set up loop (iteration 1 was the initial prompt)
        event = {
            "hook_event_name": "Stop",
            "transcript_path": transcript,
            "last_assistant_message": "Starting work.",
        }
        d = self._run_hook(proj, event)
        assert d["decision"] == "block"
        state = self._read_loop_file(dot_claude)
        assert state["remaining"] == 4  # 5 - 1
        assert state["total"] == 5

        # 3. Second hook: normal continuation
        event["last_assistant_message"] = "Created the file."
        d = self._run_hook(proj, event)
        assert d["decision"] == "block"
        state = self._read_loop_file(dot_claude)
        assert state["remaining"] == 3

        # 4. Third hook: agent claims complete -> verification
        event["last_assistant_message"] = "All done. TASK_COMPLETE"
        d = self._run_hook(proj, event)
        assert "Verification" in d["reason"]
        state = self._read_loop_file(dot_claude)
        assert state["remaining"] == 2

        # 5. Fourth hook: verification passes -> loop ends
        event["last_assistant_message"] = "Verified. REVIEW_OKAY"
        d = self._run_hook(proj, event)
        assert "verified" in d["reason"].lower()
        assert self._read_loop_file(dot_claude) is None
