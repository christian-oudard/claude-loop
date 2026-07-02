"""Unit and integration tests for persist core logic."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
import persist

from helpers import (
    make_project, read_session, write_session,
    run_main, run_start, run_status, run_status_short, run_active, run_hook,
    run_persist, make_stop_event, make_pre_tool_use_event,
)


# --- Unit tests: parse_limit ---

class TestParseLimit:
    def test_iterations(self):
        total, deadline = persist.parse_limit("5")
        assert total == 5
        assert deadline is None

    def test_max_iterations(self):
        total, deadline = persist.parse_limit("999")
        assert total == 999
        assert deadline is None

    def test_hours(self):
        before = time.time()
        total, deadline = persist.parse_limit("2h")
        assert total is None
        assert before + 7200 <= deadline <= time.time() + 7200

    def test_minutes(self):
        before = time.time()
        total, deadline = persist.parse_limit("30m")
        assert total is None
        assert before + 1800 <= deadline <= time.time() + 1800

    def test_military_time(self):
        total, deadline = persist.parse_limit("1400")
        assert total is None
        assert deadline is not None

    def test_colon_time(self):
        total, deadline = persist.parse_limit("14:00")
        assert total is None
        assert deadline is not None

    def test_pm(self):
        total, deadline = persist.parse_limit("2pm")
        assert total is None
        assert deadline is not None

    def test_am(self):
        total, deadline = persist.parse_limit("11am")
        assert total is None
        assert deadline is not None

    def test_invalid_military_time(self):
        import pytest
        with pytest.raises(ValueError):
            persist.parse_limit("2500")

    def test_zero_iterations(self):
        import pytest
        with pytest.raises(ValueError):
            persist.parse_limit("0")

    def test_forever_keyword(self):
        total, deadline = persist.parse_limit("forever")
        assert total is None
        assert deadline is None

    def test_forever_case_insensitive(self):
        total, deadline = persist.parse_limit("Forever")
        assert total is None
        assert deadline is None


# --- Unit tests: is_expired ---

class TestIsExpired:
    def test_iteration_not_expired(self):
        assert persist.is_expired({"iteration": 3, "total": 5}) is None

    def test_iteration_expired(self):
        assert persist.is_expired({"iteration": 6, "total": 5}) == 'iterations'

    def test_deadline_not_expired(self):
        assert persist.is_expired({"deadline": time.time() + 3600}) is None

    def test_deadline_expired(self):
        assert persist.is_expired({"deadline": time.time() - 1}) == 'deadline'

    def test_no_limits(self):
        assert persist.is_expired({"iteration": 1}) is None


# --- Unit tests: find_keyword ---

class TestFindKeyword:
    def test_task_complete(self):
        assert persist.find_keyword("blah TASK_COMPLETE blah") == "TASK_COMPLETE"

    def test_review_okay(self):
        assert persist.find_keyword("REVIEW_OKAY") == "REVIEW_OKAY"

    def test_review_incomplete(self):
        assert persist.find_keyword("some text REVIEW_INCOMPLETE more") == "REVIEW_INCOMPLETE"

    def test_no_keyword(self):
        assert persist.find_keyword("just normal text") is None

    def test_empty(self):
        assert persist.find_keyword("") is None

    def test_priority_review_okay_first(self):
        """REVIEW_OKAY beats TASK_COMPLETE when both appear in text."""
        assert persist.find_keyword("TASK_COMPLETE REVIEW_OKAY") == "REVIEW_OKAY"

    def test_priority_review_incomplete_over_task_complete(self):
        assert persist.find_keyword("TASK_COMPLETE REVIEW_INCOMPLETE") == "REVIEW_INCOMPLETE"


# --- Integration tests: start() ---

class TestStart:
    def test_basic_start(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        result = run_start(proj, "5 Fix the bug")
        assert result.returncode == 0
        state = read_session(dot_claude)
        assert state["iteration"] == 0
        assert state["prompt"] == "Fix the bug"
        assert state["total"] == 5

    def test_start_outputs_work_prompt(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        result = run_start(proj, "3 Do stuff")
        assert "Iteration 1" in result.stdout
        assert "Do stuff" in result.stdout

    def test_first_iteration_has_loop_intro(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        result = run_start(proj, "3 Do stuff")
        assert "persistent coding loop" in result.stdout
        assert "same prompt again" in result.stdout

    def test_subsequent_iteration_omits_loop_intro(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 1, "Do stuff", 5)
        decision = run_hook(proj, make_stop_event("Progress."))
        assert "same prompt again" not in decision["reason"]

    def test_start_writes_session(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        run_start(proj, "5 task")
        assert read_session(dot_claude) is not None

    def test_start_replaces_existing_session(self, tmp_path):
        """A second /persist:go immediately overwrites the prior session."""
        proj, dot_claude = make_project(tmp_path)
        run_start(proj, "5 task A")
        run_start(proj, "3 task B")
        state = read_session(dot_claude)
        assert state["prompt"] == "task B"
        assert state["total"] == 3

    def test_multiline_task(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        run_start(proj, "3 Fix the bug.\nAlso update tests.")
        assert read_session(dot_claude)["prompt"] == "Fix the bug.\nAlso update tests."

    def test_curly_braces(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        run_start(proj, "2 Fix the {name} field")
        assert read_session(dot_claude)["prompt"] == "Fix the {name} field"

    def test_shell_metacharacters(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        run_start(proj, "1 echo $HOME && rm -rf /; don't")
        assert read_session(dot_claude)["prompt"] == "echo $HOME && rm -rf /; don't"

    def test_quotes(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        run_start(proj, """2 Fix the "parser" and it's 'edge cases'""")
        assert read_session(dot_claude)["prompt"] == """Fix the "parser" and it's 'edge cases'"""

    def test_no_project_root(self, tmp_path):
        result = run_start(tmp_path, "3 Do stuff")
        assert result.returncode == 1
        assert "Not in a project" in result.stderr

    def test_auto_creates_dot_claude(self, tmp_path):
        (tmp_path / ".git").mkdir()
        result = run_start(tmp_path, "3 Do stuff")
        assert result.returncode == 0
        assert (tmp_path / ".claude").is_dir()

    def test_empty_stdin(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        result = run_start(proj, "")
        assert result.returncode == 1
        assert "Usage" in result.stderr

    def test_missing_task(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        result = run_start(proj, "5")
        assert result.returncode == 1
        assert "Usage" in result.stderr

    def test_time_limit_start(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        result = run_start(proj, "2h Fix the bug")
        assert result.returncode == 0
        data = read_session(dot_claude)
        assert data["prompt"] == "Fix the bug"
        assert data["total"] is None
        assert data["deadline"] is not None
        assert data["deadline"] > time.time()

    def test_start_records_started_time(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        before = time.time()
        run_start(proj, "5 Do stuff")
        data = read_session(dot_claude)
        assert data["started"] is not None
        assert before <= data["started"] <= time.time()

    def test_status_active(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 3, "Fix the bug", 5)
        result = run_status(proj)
        assert result.returncode == 0
        assert "Fix the bug" in result.stdout

    def test_status_inactive(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        result = run_status(proj)
        assert result.returncode == 0
        assert "No active session" in result.stdout

    def test_status_does_not_create_dot_claude(self, tmp_path):
        (tmp_path / ".git").mkdir()
        result = run_status(tmp_path)
        assert result.returncode == 0
        assert "No active session" in result.stdout
        assert not (tmp_path / ".claude").exists()

    def test_status_short_active(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 3, "Fix the bug", 5)
        result = run_status_short(proj)
        assert result.returncode == 0
        line = result.stdout.strip()
        assert line.startswith("persist ")
        assert not line.startswith("persist --lock")
        assert "3/5" in line

    def test_status_short_locked(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 3, "Fix the bug", 5, lock=True)
        result = run_status_short(proj)
        assert result.returncode == 0
        assert result.stdout.strip().startswith("persist --lock ")

    def test_status_short_inactive(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        result = run_status_short(proj)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_status_short_done(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 3, "Fix the bug", 5)
        state = read_session(dot_claude)
        state["done"] = True
        (dot_claude / "persist.json").write_text(json.dumps(state))
        result = run_status_short(proj)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_active_with_session(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 3, "Fix the bug", 5)
        result = run_active(proj)
        assert result.returncode == 0
        assert result.stdout == ""


class TestMain:
    def test_help_flag(self, tmp_path):
        result = run_main(tmp_path, ["--help"])
        assert result.returncode == 0
        assert "persist status" in result.stdout

    def test_h_flag(self, tmp_path):
        result = run_main(tmp_path, ["-h"])
        assert result.returncode == 0
        assert "persist status" in result.stdout

    def test_help_flag_after_subcommand(self, tmp_path):
        result = run_main(tmp_path, ["status", "--help"])
        assert result.returncode == 0
        assert "persist status" in result.stdout
        assert "No active session" not in result.stdout

    def test_unknown_command(self, tmp_path):
        result = run_main(tmp_path, ["bogus"])
        assert result.returncode == 2
        assert "unknown command: bogus" in result.stderr

    def test_active_no_session_no_side_effects(self, tmp_path):
        (tmp_path / ".git").mkdir()
        result = run_active(tmp_path)
        assert result.returncode == 1
        assert not (tmp_path / ".claude").exists()

    def test_active_expired_reads_inactive(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 1, "Fix the bug", deadline=1)
        result = run_active(proj)
        assert result.returncode == 1

    def test_active_corrupt_state_undeterminable(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        (dot_claude / "persist.json").write_text("{ not valid json")
        result = run_active(proj)
        assert result.returncode == 2


# --- Integration tests: hook state machine ---

class TestHookStateMachine:
    def test_normal_continuation(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 1, "Do the thing", 5)

        decision = run_hook(proj, make_stop_event("Made some progress."))

        assert decision["decision"] == "block"
        assert read_session(dot_claude)["iteration"] == 2

    def test_task_complete_triggers_verification(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 2, "Build feature X", 5)

        decision = run_hook(proj, make_stop_event("Done! TASK_COMPLETE"))

        assert decision["decision"] == "block"
        assert "Verification" in decision["reason"]

    def test_review_okay_ends_session(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 3, "Build feature X", 5)

        # REVIEW_OKAY starts the final summary turn: session kept (done), silent.
        decision = run_hook(proj, make_stop_event("Everything looks good. REVIEW_OKAY"))
        assert decision["decision"] == "block"
        assert "verified" in decision["reason"].lower()
        assert read_session(dot_claude)["done"] is True

        # Next stop tears it down silently (no decision); the bell rings.
        decision = run_hook(proj, make_stop_event("Summary of the work."))
        assert decision is None
        assert read_session(dot_claude) is None

    def test_review_okay_beats_iterations_exhausted(self, tmp_path):
        """REVIEW_OKAY takes priority even when iterations are also exhausted."""
        proj, dot_claude = make_project(tmp_path)
        # iteration=1, total=1 → next iteration (2) exceeds total
        write_session(dot_claude, 1, "debug", 1)

        decision = run_hook(proj, make_stop_event(
            "Looks good. REVIEW_OKAY"))

        assert decision["decision"] == "block"
        assert "verified" in decision["reason"].lower()
        assert "exhausted" not in decision["reason"].lower()
        assert read_session(dot_claude)["done"] is True

    def test_review_incomplete_continues(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 3, "Build feature X", 5)

        decision = run_hook(proj, make_stop_event("Found a bug. REVIEW_INCOMPLETE"))

        assert decision["decision"] == "block"
        assert read_session(dot_claude)["iteration"] == 4

    def test_iterations_exhausted(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 3, "Do stuff", 3)

        decision = run_hook(proj, make_stop_event("Still working..."))

        assert "exhausted" in decision["reason"].lower()
        assert read_session(dot_claude)["done"] is True
        assert run_hook(proj, make_stop_event("Summary.")) is None
        assert read_session(dot_claude) is None

    def test_no_state_silent(self, tmp_path):
        proj, _ = make_project(tmp_path)
        decision = run_hook(proj, make_stop_event("Hello"))
        assert decision is None

    def test_non_stop_event_ignored(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 3, "task", 5)

        decision = run_hook(proj, {"hook_event_name": "NotStop",
                                    "last_assistant_message": ""})
        assert decision is None
        assert read_session(dot_claude)["iteration"] == 3

    def test_curly_braces_in_prompt(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 2, "Fix the {name} field", 5)
        decision = run_hook(proj, make_stop_event("Working on it."))
        assert "Fix the {name} field" in decision["reason"]

    def test_curly_braces_in_verification(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 2, "Update {foo} and {bar}", 5)
        decision = run_hook(proj, make_stop_event("Done! TASK_COMPLETE"))
        assert "Update {foo} and {bar}" in decision["reason"]

    def test_multiline_prompt_in_hook(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        task = "Step 1: fix parsing.\nStep 2: add tests.\nStep 3: deploy."
        write_session(dot_claude, 2, task, 5)
        decision = run_hook(proj, make_stop_event("Progress made."))
        assert task in decision["reason"]

    def test_full_lifecycle(self, tmp_path):
        """Full start -> hook iterations -> task complete -> done."""
        proj, dot_claude = make_project(tmp_path)

        # 1. Start: writes session
        result = run_start(proj, "5 Create hello.txt")
        assert result.returncode == 0
        assert read_session(dot_claude)["iteration"] == 0

        # 2. First Stop: iteration 1
        d = run_hook(proj, make_stop_event("Starting work."))
        assert d["decision"] == "block"
        assert read_session(dot_claude)["iteration"] == 1

        # 3. Second Stop: iteration 2
        d = run_hook(proj, make_stop_event("Created the file."))
        assert read_session(dot_claude)["iteration"] == 2

        # 4. TASK_COMPLETE -> verification
        d = run_hook(proj, make_stop_event("All done. TASK_COMPLETE"))
        assert "Verification" in d["reason"]

        # 5. REVIEW_OKAY -> final summary turn (session kept, marked done)
        d = run_hook(proj, make_stop_event("Verified. REVIEW_OKAY"))
        assert "verified" in d["reason"].lower()
        assert read_session(dot_claude)["done"] is True

        # 6. Summary turn ends -> teardown, session gone
        d = run_hook(proj, make_stop_event("Here is what I did."))
        assert d is None
        assert read_session(dot_claude) is None

    def test_deadline_expired_ends_session(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 2, "Do stuff",
                      deadline=time.time() - 1)
        decision = run_hook(proj, make_stop_event("Still working..."))
        assert "time limit" in decision["reason"].lower()
        assert read_session(dot_claude)["done"] is True
        assert run_hook(proj, make_stop_event("Summary.")) is None
        assert read_session(dot_claude) is None

    def test_deadline_not_expired_continues(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 2, "Do stuff",
                      deadline=time.time() + 3600)
        decision = run_hook(proj, make_stop_event("Making progress."))
        assert "Iteration" in decision["reason"]
        assert read_session(dot_claude)["iteration"] == 3


# --- PreToolUse: block self-stop ---

class TestBlockSelfStop:
    def test_blocks_bash_persist_stop(self, tmp_path):
        """Agent in a persist session cannot run 'persist stop' via Bash."""
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 2, "Fix bugs", 5)

        event = make_pre_tool_use_event(
            "Bash", {"command": "persist stop"})
        decision = run_hook(proj, event)

        assert decision["decision"] == "block"
        assert "Cannot stop" in decision["reason"]
        # Session still exists
        assert read_session(dot_claude) is not None

    def test_blocks_skill_persist_stop(self, tmp_path):
        """Agent in a persist session cannot invoke the persist:stop skill."""
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 2, "Fix bugs", 5)

        event = make_pre_tool_use_event(
            "Skill", {"skill": "persist:stop"})
        decision = run_hook(proj, event)

        assert decision["decision"] == "block"
        assert "Cannot stop" in decision["reason"]

    def test_allows_non_persist_session(self, tmp_path):
        """Bash 'persist stop' is allowed when session is NOT persisted."""
        proj, dot_claude = make_project(tmp_path)

        event = make_pre_tool_use_event(
            "Bash", {"command": "persist stop"})
        decision = run_hook(proj, event)

        assert decision is None

    def test_allows_other_bash_commands(self, tmp_path):
        """Non-stop Bash commands are not blocked."""
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 2, "Fix bugs", 5)

        event = make_pre_tool_use_event(
            "Bash", {"command": "persist status"})
        decision = run_hook(proj, event)

        assert decision is None

    def test_allows_other_skills(self, tmp_path):
        """Non-stop skills are not blocked."""
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 2, "Fix bugs", 5)

        event = make_pre_tool_use_event(
            "Skill", {"skill": "persist:status"})
        decision = run_hook(proj, event)

        assert decision is None


class TestStartedField:
    def test_started_preserved_through_iterations(self, tmp_path):
        """started timestamp survives stop_hook state updates."""
        proj, dot_claude = make_project(tmp_path)
        started = time.time() - 3600  # 1 hour ago
        write_session(dot_claude, 2, "Do stuff", total=5,
                      started=started)

        run_hook(proj, make_stop_event("Progress."))
        data = read_session(dot_claude)
        assert data["started"] == started
        assert data["iteration"] == 3

    def test_iteration_label_is_plain_number(self, tmp_path):
        """Work prompt shows just the iteration number, no elapsed time."""
        proj, dot_claude = make_project(tmp_path)
        started = time.time() - 5400  # 1h30m ago
        write_session(dot_claude, 2, "Do stuff", total=10,
                      started=started)

        decision = run_hook(proj, make_stop_event("Progress."))
        assert "Iteration 3\n" in decision["reason"]
        assert "1h" not in decision["reason"]

    def test_started_preserved_through_task_complete(self, tmp_path):
        """started preserved when TASK_COMPLETE triggers verification."""
        proj, dot_claude = make_project(tmp_path)
        started = time.time() - 1800
        write_session(dot_claude, 2, "Build it", total=5,
                      started=started)

        run_hook(proj, make_stop_event("Done! TASK_COMPLETE"))
        data = read_session(dot_claude)
        assert data["started"] == started


class TestFormatRemaining:
    def test_iteration_based(self):
        assert persist.format_remaining({"iteration": 3, "total": 5}) == "3/5"

    def test_deadline_with_started(self):
        now = time.time()
        result = persist.format_remaining({
            "iteration": 2,
            "deadline": now + 3600,
            "started": now - 3600,
        })
        # Should show elapsed/total like "2, 1h00m/2h00m"
        assert "2, " in result
        assert "/" in result
        assert "remaining" not in result

    def test_deadline_without_started(self):
        result = persist.format_remaining({
            "iteration": 2,
            "deadline": time.time() + 3600,
        })
        assert "remaining" in result

    def test_deadline_expired(self):
        result = persist.format_remaining({
            "iteration": 5,
            "deadline": time.time() - 100,
            "started": time.time() - 7300,
        })
        assert "expired" in result


class TestForever:
    def test_start_forever_keyword(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        result = run_start(proj, "forever Fix the bug")
        assert result.returncode == 0
        state = read_session(dot_claude)
        assert state["total"] is None
        assert state["deadline"] is None

    def test_forever_never_expires(self, tmp_path):
        """Forever session continues indefinitely."""
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 100, "Fix bugs")

        decision = run_hook(proj, make_stop_event("Still going."))
        assert decision["decision"] == "block"
        assert "Iteration" in decision["reason"]
        assert read_session(dot_claude)["iteration"] == 101

    def test_forever_task_complete_triggers_verification(self, tmp_path):
        """Forever session still honors TASK_COMPLETE."""
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 5, "Fix bugs")

        decision = run_hook(proj, make_stop_event("Done! TASK_COMPLETE"))
        assert "Verification" in decision["reason"]

    def test_forever_review_okay_ends(self, tmp_path):
        """Forever session ends on REVIEW_OKAY."""
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 5, "Fix bugs")

        decision = run_hook(proj, make_stop_event("REVIEW_OKAY"))
        assert "verified" in decision["reason"].lower()
        assert read_session(dot_claude)["done"] is True

    def test_forever_lock_truly_infinite(self, tmp_path):
        """forever + --lock: only /persist:stop can end it."""
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 50, "Fix bugs", lock=True)

        decision = run_hook(proj, make_stop_event("TASK_COMPLETE REVIEW_OKAY"))
        # Should continue despite both keywords
        assert "Iteration" in decision["reason"]
        assert read_session(dot_claude)["iteration"] == 51

    def test_forever_format_remaining(self):
        result = persist.format_remaining({"iteration": 7, "started": time.time() - 3600})
        assert "forever" in result
        assert "7, " in result

    def test_forever_format_remaining_no_started(self):
        result = persist.format_remaining({"iteration": 3})
        assert "forever" in result


class TestLock:
    def test_start_with_lock(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        result = run_start(proj, "--lock 5 Fix the bug")
        assert result.returncode == 0
        state = read_session(dot_claude)
        assert state["lock"] is True
        assert state["prompt"] == "Fix the bug"
        assert state["total"] == 5

    def test_start_lock_after_limit(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        result = run_start(proj, "5 --lock Fix the bug")
        assert result.returncode == 0
        state = read_session(dot_claude)
        assert state["lock"] is True
        assert state["prompt"] == "Fix the bug"

    def test_start_lock_short_flag(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        result = run_start(proj, "-l 5 Fix the bug")
        assert result.returncode == 0
        state = read_session(dot_claude)
        assert state["lock"] is True
        assert state["prompt"] == "Fix the bug"

    def test_lock_prompt_omits_task_complete(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        result = run_start(proj, "--lock 3 Do stuff")
        assert "TASK_COMPLETE" not in result.stdout
        assert "locked session" in result.stdout.lower()
        assert "no completion keyword" in result.stdout.lower()

    def test_lock_ignores_task_complete(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 2, "Build it", total=5, lock=True)

        decision = run_hook(proj, make_stop_event("Done! TASK_COMPLETE"))
        # Should continue, not trigger verification
        assert "Verification" not in decision["reason"]
        assert "Iteration" in decision["reason"]
        assert "locked session" in decision["reason"].lower()
        assert "next most valuable" in decision["reason"].lower()
        assert read_session(dot_claude)["iteration"] == 3

    def test_lock_ignores_review_okay(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 2, "Build it", total=5, lock=True)

        decision = run_hook(proj, make_stop_event("REVIEW_OKAY"))
        # Should continue, not end session
        assert "verified" not in decision["reason"].lower()
        assert "locked session" in decision["reason"].lower()
        assert read_session(dot_claude) is not None
        assert read_session(dot_claude)["iteration"] == 3

    def test_lock_still_expires_on_iterations(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 3, "Do stuff", total=3, lock=True)

        decision = run_hook(proj, make_stop_event("TASK_COMPLETE"))
        assert "exhausted" in decision["reason"].lower()
        assert read_session(dot_claude)["done"] is True
        assert run_hook(proj, make_stop_event("Summary.")) is None
        assert read_session(dot_claude) is None

    def test_lock_still_expires_on_deadline(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 2, "Do stuff",
                      deadline=time.time() - 1, lock=True)

        decision = run_hook(proj, make_stop_event("REVIEW_OKAY"))
        assert "time limit" in decision["reason"].lower()
        assert read_session(dot_claude)["done"] is True

    def test_lock_preserved_through_iterations(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 1, "Build it", total=5, lock=True)

        run_hook(proj, make_stop_event("Progress."))
        state = read_session(dot_claude)
        assert state["lock"] is True
        assert state["iteration"] == 2

    def test_lock_work_prompt_in_hook(self, tmp_path):
        """Normal stop in lock mode has no TASK_COMPLETE mention at all."""
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 1, "Build it", total=5, lock=True)

        decision = run_hook(proj, make_stop_event("Progress."))
        assert "TASK_COMPLETE" not in decision["reason"]


class TestFinishingState:
    """The teardown keeps the session live for one final summary turn, so the
    bell guard (`persist active`) does not flip until the review cycle is over."""

    def _done(self, dot_claude):
        (dot_claude / "persist.json").write_text(json.dumps(
            {"iteration": 3, "prompt": "x", "started": None, "done": True}))

    def test_done_session_reads_active(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        self._done(dot_claude)
        assert run_active(proj).returncode == 0

    def test_done_session_stop_deletes_silently(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        self._done(dot_claude)
        decision = run_hook(proj, make_stop_event("Summary."))
        assert decision is None
        assert read_session(dot_claude) is None

    def test_status_shows_finishing(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        self._done(dot_claude)
        assert "summar" in run_status(proj).stdout.lower()

    def test_active_never_flips_during_review_cycle(self, tmp_path):
        proj, dot_claude = make_project(tmp_path)
        write_session(dot_claude, 2, "Build X", 5)

        # TASK_COMPLETE -> verification turn; still active.
        run_hook(proj, make_stop_event("Done. TASK_COMPLETE"))
        assert run_active(proj).returncode == 0

        # REVIEW_OKAY -> summary turn; still active.
        run_hook(proj, make_stop_event("Verified. REVIEW_OKAY"))
        assert run_active(proj).returncode == 0

        # Summary turn ends -> now inactive, the bell may ring.
        run_hook(proj, make_stop_event("Here is the summary."))
        assert run_active(proj).returncode == 1


