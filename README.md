# claude-loop

An improved coding loop for Claude Code

## How it works

`/loop N TASK` gives Claude N iterations to complete a task. After each iteration, a stop hook re-injects the task. When Claude claims the task is done (`TASK_COMPLETE`), a verification iteration forces it to re-read its code, re-run tests, and confirm (`REVIEW_OKAY`) or find more work (`REVIEW_INCOMPLETE`).

```
Work iteration → TASK_COMPLETE    → verification prompt
Verification   → REVIEW_OKAY     → done
Verification   → REVIEW_INCOMPLETE → back to work
Any iteration  → n hits 0         → done (iterations exhausted)
```

## Install

With uv:
```bash
uv tool install claude-loop
```

Or with pipx:
```bash
pipx install claude-loop
```

## Setup

### 1. Add the slash command

Copy `commands/loop.md` to `~/.claude/commands/loop.md`:


### 2. Add the stop hook

Add this to your `~/.claude/settings.json` under `"hooks"`:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "claude-loop hook"
          }
        ]
      }
    ]
  }
}
```

## Usage

In Claude Code:

```
/loop 10 Implement a function that solves the traveling salesman problem
```

The first argument is the maximum number of iterations. The rest is the task description.

To cancel a running loop:

```
/loop stop
```

Or run `claude-loop stop` from a terminal in the project directory.
