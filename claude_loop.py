#!/usr/bin/env python3
"""Coding loop for Claude Code.

Provides a stop hook that re-prompts Claude with the task after each iteration,
with a verification step before completion.

Usage:
    claude-loop           Start the loop (called by /loop slash command)
    claude-loop hook      Stop hook handler (called by Claude Code)
    claude-loop stop      Cancel a running loop
"""

from pathlib import Path
import json
import sys

WORK_PROMPT = """\
# Loop iteration {iteration}

You are in a coding loop. Orient yourself by reading files and checking \
git status/log. Work incrementally: implement one piece, verify it works, \
then stop. You will be re-prompted after each iteration.

If the task is genuinely and fully complete, output exactly TASK_COMPLETE \
as a standalone message. Do not use it to escape the loop because you are \
stuck — use the next iteration to try a different approach.

## Task

{prompt}
"""

VERIFICATION_PROMPT = """\
# Verification

You indicated the task is complete. Before confirming, do a thorough review:

1. Re-read the original task requirements below.
2. Read through all code you wrote or modified.
3. Run the tests or otherwise verify the implementation works end-to-end.
4. Check for edge cases, missing requirements, or loose ends.

After your review, output exactly one of these keywords as a standalone \
message:

- REVIEW_OKAY — the task is fully and genuinely complete.
- REVIEW_INCOMPLETE — you found something incomplete or broken. Briefly \
describe what remains before the keyword.

## Task

{prompt}
"""


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'hook':
        hook()
    elif len(sys.argv) > 1 and sys.argv[1] == 'stop':
        delete_loop_file()
        print('Loop stopped.')
    else:
        start()


def start():
    if dot_claude_dir() is None:
        print("Not in a project, or there is no .claude directory.", file=sys.stderr)
        sys.exit(1)

    raw = sys.stdin.read().strip() if not sys.stdin.isatty() else ''

    if raw == 'stop':
        delete_loop_file()
        print('Loop stopped.')
        return

    # Don't overwrite an active loop.
    if read_loop_file():
        return

    if not raw:
        print("Usage: /loop NUM_ITERATIONS TASK", file=sys.stderr)
        sys.exit(1)

    parts = raw.split(None, 1)
    if len(parts) < 2:
        print("Usage: /loop NUM_ITERATIONS TASK", file=sys.stderr)
        sys.exit(1)

    total = int(parts[0])
    prompt = parts[1]
    # The initial Claude response (from the slash command text) is iteration 1.
    write_loop_file(1, prompt, total)


def hook():
    # Check whether there is a loop file.
    loop_data = read_loop_file()
    if loop_data is None:
        return

    # Only catch Stop hooks.
    event = json.loads(sys.stdin.read())
    if event['hook_event_name'] != 'Stop':
        return

    prompt = loop_data['prompt']
    iteration = loop_data['iteration'] + 1
    total = loop_data['total']

    # Check whether there was a completion keyword given by the agent.
    last_msg = event.get('last_assistant_message', '')
    keyword = find_keyword(last_msg)

    if iteration > total:
        # Iterations exhausted — end the loop.
        delete_loop_file()
        print(json.dumps({
            "decision": "block",
            "reason": "Loop complete (iterations exhausted). Summarize what you accomplished.",
        }))
    elif keyword == 'REVIEW_OKAY':
        # Verified complete — end the loop.
        delete_loop_file()
        print(json.dumps({
            "decision": "block",
            "reason": "Loop complete (verified). Summarize what you accomplished.",
        }))
    elif keyword == 'TASK_COMPLETE':
        # First claim — enter verification iteration.
        write_loop_file(iteration, prompt, total)
        print(json.dumps({
            "decision": "block",
            "reason": VERIFICATION_PROMPT.format(prompt=prompt),
        }))
    else:
        # Normal continuation. Covers REVIEW_INCOMPLETE, no keyword, and first call.
        write_loop_file(iteration, prompt, total)
        reason = WORK_PROMPT.format(prompt=prompt, iteration=iteration)
        print(json.dumps({
            "decision": "block",
            "reason": reason,
        }))


# Loop file management.

def read_loop_file():
    path = loop_file_path()
    if path and path.exists():
        return json.load(path.open())


def write_loop_file(iteration, prompt, total):
    json.dump({'iteration': iteration, 'prompt': prompt, 'total': total}, loop_file_path().open('w'))


def delete_loop_file():
    path = loop_file_path()
    if path:
        path.unlink(missing_ok=True)


def loop_file_path():
    d = dot_claude_dir()
    return d / 'loop.json' if d else None


def dot_claude_dir():
    p = Path.cwd()
    for p in [p, *p.parents]:
        if p == Path.home():
            break
        dot_claude = p / '.claude'
        if dot_claude.exists():
            return dot_claude
    return None


def find_keyword(text):
    """Check text for a loop keyword.

    Returns 'TASK_COMPLETE', 'REVIEW_OKAY', 'REVIEW_INCOMPLETE', or None.
    """
    for kw in ('TASK_COMPLETE', 'REVIEW_OKAY', 'REVIEW_INCOMPLETE'):
        if kw in text:
            return kw
    return None


if __name__ == '__main__':
    main()
