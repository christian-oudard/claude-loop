"""Persist session — re-injects the same purpose prompt each iteration."""

import json
import sys
import time

from .common import dot_claude_dir, parse_limit, is_expired


LOOP_INTRO = """\
You are in a persistent coding loop. Each time you stop, you will receive \
this same prompt again. Your work persists in files and git history."""

LOCK_NOTICE = """\
This is a locked session. There is no completion tag, and there is \
more work to do. Each iteration, think about what the next most valuable \
thing to work on is."""

CONTINUATION = """\
Check git log and recent files to see what previous iterations accomplished. \
Then ask: what is the most valuable thing I could work on next?"""

ORIENTATION = """\
Work incrementally: do one thing, verify it, then stop. Difficulty is \
expected; each setback narrows the problem. If everything obvious is \
done, look deeper."""

EXIT_INSTRUCTIONS = """\
If the purpose is genuinely and fully achieved, end your message with the \
tag <TASK_COMPLETE> alone on its own line. Only send it when it is unambiguous \
that your task is actually complete. If you are stuck or frustrated, do not \
use it to bail out; instead, the next iteration is a fresh chance to try \
something different."""

VERIFICATION = """\
You claimed the purpose is achieved. Before confirming, do a thorough review:

1. Re-read the original purpose below.
2. Read through all code you wrote or modified.
3. Run the tests or otherwise verify the implementation works end-to-end.
4. Check for edge cases, missing requirements, or loose ends.

After your review, end your message with exactly one of these tags, alone \
on its own line:

- <REVIEW_OKAY> , the purpose is fully and genuinely achieved.
- <REVIEW_INCOMPLETE> , you found something incomplete or broken. Briefly \
describe what remains, and print the tag."""


def work_prompt(*, prompt, iteration_label, lock=False, first=False):
    parts = [f"# Iteration {iteration_label}"]
    if first:
        parts.append(LOOP_INTRO)
    else:
        parts.append(CONTINUATION)
    parts.append(ORIENTATION)
    if lock:
        parts.append(LOCK_NOTICE)
    else:
        parts.append(EXIT_INSTRUCTIONS)
    parts.append(f"## Purpose\n\n{prompt}")
    return "\n\n".join(parts) + "\n"


def verification_prompt(*, prompt):
    return "\n\n".join(["# Verification", VERIFICATION, f"## Purpose\n\n{prompt}"]) + "\n"


def _iteration_label(iteration):
    """Format iteration label like '3'."""
    return str(iteration)


# --- State file ---

def _state_path(create=False):
    d = dot_claude_dir(create)
    return d / 'persist.json' if d else None


def read_session():
    path = _state_path()
    if path and path.exists():
        return json.load(path.open())
    return None


def write_session(state):
    path = _state_path(create=True)
    json.dump(state, path.open('w'))


def delete_session():
    path = _state_path()
    if path and path.exists():
        path.unlink()


# --- Commands ---

def start():
    if dot_claude_dir(create=True) is None:
        print("Not in a project, or there is no .claude directory.", file=sys.stderr)
        sys.exit(1)

    raw = sys.stdin.read().strip() if not sys.stdin.isatty() else ''

    if not raw:
        print("Usage: /persist:go [--lock|-l] LIMIT PURPOSE", file=sys.stderr)
        sys.exit(1)

    tokens = raw.split()
    lock = '--lock' in tokens or '-l' in tokens
    if lock:
        tokens.remove('--lock' if '--lock' in tokens else '-l')
        raw = ' '.join(tokens)

    parts = raw.split(None, 1)
    if len(parts) < 2:
        print("Usage: /persist:go [--lock|-l] LIMIT PURPOSE", file=sys.stderr)
        sys.exit(1)

    now = time.time()
    total, deadline = parse_limit(parts[0], now=now)
    prompt = parts[1]
    # Iteration 1 is the turn this prompt starts, so the state records 1 and
    # each stop hook moves to the next number. Starting at 0 would label the
    # first hook's prompt "1" a second time, and run one turn past the limit.
    state = {
        'iteration': 1, 'prompt': prompt,
        'total': total, 'deadline': deadline,
        'started': now,
    }
    if lock:
        state['lock'] = True
    # Immediately replaces any existing session.
    write_session(state)
    print(work_prompt(prompt=prompt, iteration_label="1", lock=lock, first=True))


def stop():
    delete_session()
    print('Session stopped.')


def status(short=False):
    from .common import format_remaining
    state = read_session()
    if short:
        if state and not state.get('done'):
            prefix = 'persist --lock' if state.get('lock') else 'persist'
            print(f"{prefix} {format_remaining(state)}")
        return
    if not state:
        print("No active session.")
        return
    if state.get('done'):
        print("Session complete; summarizing.")
        return
    print(f"Iteration {format_remaining(state)}")
    print(f"Purpose: {state['prompt']}")


def active():
    """Exit 0 if a live session is running, 1 if none, 2 if undeterminable.

    Silent. Checks expiry, so a stale state file past its limit reads as no
    live session (1). Any error reading the state, e.g. corrupt JSON, exits
    2 so a guard can tell "no session" apart from "cannot determine." This
    is the predicate external tooling guards on, e.g. a stop-bell hook.

    The 50ms delay lets the loop hook win the read/delete race on the final
    teardown stop: both Stop hooks fire in parallel, so without it the bell
    guard could read the session before the loop hook deletes it and stay
    silent, missing the completion ring. The loop hook deletes first thing,
    so this delay reliably observes the post-deletion state.
    """
    time.sleep(0.05)
    try:
        state = read_session()
        live = bool(state) and not is_expired(state)
    except Exception:
        sys.exit(2)
    sys.exit(0 if live else 1)


def _next_state(state, iteration):
    """Build the next iteration's state dict, preserving all fields."""
    return {
        'iteration': iteration, 'prompt': state['prompt'],
        'total': state.get('total'), 'deadline': state.get('deadline'),
        'started': state.get('started'),
        **({'lock': True} if state.get('lock') else {}),
    }


def _finish(state, reason):
    """End the loop with one final summary turn.

    The session is kept (marked done, limits stripped so it still reads as
    live) rather than deleted, so the bell guard stays silent on this stop:
    `persist active` must not flip until the whole review cycle is over. The
    next stop runs the done branch, which deletes and lets the bell ring once.
    """
    write_session({
        'iteration': state['iteration'],
        'prompt': state['prompt'],
        'started': state.get('started'),
        'done': True,
    })
    print(json.dumps({"decision": "block", "reason": reason}))


def stop_hook(state, event):
    """Handle a stop hook for the active persist session."""
    # Final teardown stop: the summary turn just finished. Delete first thing
    # so the bell guard's delayed `active` read reliably sees no session and
    # rings exactly once.
    if state.get('done'):
        delete_session()
        return

    prompt = state['prompt']
    iteration = state['iteration'] + 1
    lock = state.get('lock')

    last_msg = event.get('last_assistant_message', '')
    keyword = find_keyword(last_msg)

    expired = is_expired({**state, 'iteration': iteration})

    if not lock and keyword == 'REVIEW_OKAY':
        _finish(state, "Session complete (verified). Summarize what you accomplished.")
    elif expired:
        reason = 'time limit reached' if expired == 'deadline' else 'iterations exhausted'
        _finish(state, f"Session complete ({reason}). Summarize what you accomplished.")
    elif not lock and keyword == 'TASK_COMPLETE':
        write_session(_next_state(state, iteration))
        print(json.dumps({
            "decision": "block",
            "reason": verification_prompt(prompt=prompt),
        }))
    else:
        write_session(_next_state(state, iteration))
        wp = work_prompt(prompt=prompt, iteration_label=_iteration_label(iteration), lock=lock)
        if lock and keyword:
            wp = ("You indicated you are done, however this is a locked "
                  "session with no completion tag. There is more work "
                  "to do. Think about what the next most valuable thing to "
                  "work on is.\n\n" + wp)
        print(json.dumps({
            "decision": "block",
            "reason": wp,
        }))


TAGS = ('<REVIEW_OKAY>', '<REVIEW_INCOMPLETE>', '<TASK_COMPLETE>')


def find_keyword(text):
    """Check text for a session signal.

    Returns 'REVIEW_OKAY', 'REVIEW_INCOMPLETE', 'TASK_COMPLETE', or None.

    A signal is a tag alone on its own line. The angle brackets keep the tag
    distinct from the bare word an agent uses when talking about the loop, and
    the own-line rule keeps a quoted or negated tag from firing. Both matter:
    an agent writing "not <TASK_COMPLETE>, the run is still going" must not
    trigger verification. The last matching line wins, so REVIEW_INCOMPLETE
    can follow a description of what remains, as its instructions ask.
    """
    matches = [line.strip() for line in text.splitlines() if line.strip() in TAGS]
    return matches[-1].strip('<>') if matches else None
