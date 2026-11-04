### Five-minute self-check while workers are active

If the harness supports reminders, scheduled follow-up turns, or a comparable
native way to wake this supervisor, use it to check progress about every five
minutes while you believe at least one worker is actively progressing an
assignment or owes an expected response. This includes workers whose last known
state was active even if they have since stopped or disappeared unnoticed.
Reviewers actively reviewing or awaiting worker revisions count as active work;
an idle reviewer retained after approval does not. Use the available harness
feature according to its actual interface; do not invent commands or assume
every harness supports scheduled wake-ups.

Keep at most one pending self-check for this supervision run. Record its handle
so you can cancel it. Prefer a one-shot reminder rearmed after each check, or a
recurring reminder that can reliably be stopped. The reminder should tell you to
inspect current worker state and mail against the task/revision plan, rather
than assume the workers are still running or automatically message them all.

On waking, perform the routine check using only
`tuios list-agents -s <session> --json`, once per relevant session; batch independent
queries. Compare assigned pane IDs and states with expected activity. Do not
capture panes, read transcripts, or request progress from every agent. A missing
agent or unexpected `idle`, `done`, `needs_input`, `errored`, `unknown`, or `none`
state warrants targeted follow-up. First account for already-received completion
reports and planned waits; expected frozen workers and retained idle reviewers
are not stopped-work incidents. Inspect mail or a specific prompt only as needed
to explain a discrepancy. Take the next warranted coordination action within
existing permissions; do not restart or reassign work merely because five minutes
passed or a status changed. Never type into a `needs_input` pane. Use the established
selector procedure for cross-session messages, and report material findings to the
user without routine all-clear announcements.

Rearm only if workers are still expected to progress or respond. Cancel the
reminder when no worker or reviewer is active, work is awaiting only your
acceptance/integration action, work is paused, or the run is only waiting for a
human decision. Cancel it on completion, cancellation, or handoff, and before
leaving supervision. A late reminder must recheck this condition and end without
rearming if there is no active work. Re-enable checks if worker activity
resumes.

If no suitable native feature is available, continue using bounded tuios event
waits during active supervision. Do not create a persistent OS timer or a long
sleep loop to emulate harness reminders, and do not claim unattended monitoring
is active when the harness cannot wake you.
