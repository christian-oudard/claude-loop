# persist Spec

## Purpose

persist extends Claude Code with persistent coding sessions. It re-prompts Claude after each turn via a stop hook, keeping work going across multiple iterations without manual intervention.

## Commands

- `/persist:go LIMIT PURPOSE` , start a session that re-injects PURPOSE every iteration
- `/persist:go (-l/--lock) LIMIT PURPOSE` , start a session that ignores task completion (runs until limit)
- `/persist:status` , show session status
- `/persist:stop` , stop a running session

## Limits

The LIMIT argument can be an iteration count or a time limit:

- `5` , 5 iterations (max 999)
- `2h` , 2 hours from now
- `30m` , 30 minutes from now
- `2pm` , today at 2 PM (tomorrow if already past)
- `1400` , military time, today at 14:00 (numbers >= 1000 are always time)
- `14:00` , same as above
- `forever` , no limit, runs until stopped or task complete

Time-based sessions run until the deadline, with no iteration cap. Iteration-based sessions have no time limit. Forever sessions have neither, ending only via task completion or `/persist:stop`.

## Session State

An active session is marked by the file `.claude/persist.json` in the project, created by `/persist:go` and deleted when the session ends for any reason.

`persist status` prints a human-readable summary and always exits 0, where "No active session." is itself a successful result. `persist status --short` (`-s`) prints a compact one-line form for statuslines: nothing when there is no live session, otherwise `persist <remaining>` (or `persist --lock <remaining>` for locked sessions), using the same remaining format as full status. It always exits 0. `persist active` is the machine-readable predicate: it prints nothing and exits 0 when a live session is running, 1 when there is definitely no live session, and 2 when the state cannot be determined (for example a corrupt state file). A session past its limit but not yet cleaned up counts as no live session (exit 1). External tooling guards on this with a fail-safe rule: act only on a definite exit 1. A Stop-hook bell, for instance, rings on exit 1 but stays silent both during a live loop (0) and when liveness is unknown (2), so a broken or absent predicate never causes spurious rings.

The session stays live through its entire wind-down. When work finishes or a limit is reached, persist runs one final turn that asks for a summary of what was accomplished, and only then ends. `persist active` reports the session as live across that wind-down, including the verification and summary turns, so a Stop-hook bell stays silent throughout and rings exactly once, after the summary, at the true end of the session.

## Termination

A session winds down to a final summary turn, then ends, when any of these conditions is met:

- **Iteration limit** reached
- **Deadline** reached
- **Task completion**: Claude outputs TASK_COMPLETE, then confirms with REVIEW_OKAY after the verification turn

With `--lock`, task completion keywords are ignored. The session runs until its iteration or deadline limit. If the agent attempts to use a completion keyword anyway, the prompt explicitly tells it this is a locked session and it cannot exit.

## Work Prompt

Each iteration, persist injects a work prompt that includes:

- **First iteration**: an explanation of the loop mechanism
- **Continuation iterations**: orientation via git log, asking "what is the most valuable thing I could work on next?"
- **Always**: guidance toward incremental work and persistence through difficulty
- **Unless --lock**: instructions for signaling task completion

Design principles:
- **Autonomy**: the agent decides what to work on, not the prompt
- **Purpose over compliance**: heading says "Purpose" not "Task"
- **Anti-helplessness**: continuation prompt reviews prior work, establishing that previous actions had effects
- **Growth framing**: difficulty is expected, adjust approach rather than give up
- **Factual exit**: TASK_COMPLETE is a truth claim, not a way to escape frustration
