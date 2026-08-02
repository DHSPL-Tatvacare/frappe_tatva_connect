# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W4.3 — every number the engine paces itself by, DECLARED, in one place.

Scattered across the modules that use them, a chunk size and a retention window read as implementation
detail and the engine ends up inferring its own behaviour — the defect W4 exists to delete (§11).

Constants, not a settings doctype: name who would change one AT RUNTIME and none survives the question.
The operator's controls over this engine are the SWITCHES, and they already exist and already ship OFF.

Imports nothing on purpose — `hooks.py` reads this at boot, before the app is loaded.
"""

# The cron the reliability sweep runs on; DEMOTED not deleted, it covers the ~61s scheduler-lock window.
SWEEP_CRON = "*/15 * * * *"

# Rows one sweep pass claims — a backlog drains over several passes, never one job holding a transaction.
SWEEP_PAGE = 200

# Seconds a wake job may run; a segment unfinished in 25 minutes is stuck, not slow.
WAKE_JOB_TIMEOUT = 1500

# Pending alarms above which a park stops setting one and leaves the wake to the sweep. An alarm is a COPY
# of `resume_at` held in Redis for the whole wait, and §6.2 is that a workflow entry in Redis is a pointer
# or a copy, never a fact; §5.4 declares the sweep the volume path, which it only becomes above this line.
# A DECLARED OPERATING CHOICE, not an arithmetic result — the footprint is linear and the sweep covers every
# row either side, so nothing breaks just above or below. It is set at one `SWEEP_PAGE` so the handover
# lands where a single sweep pass can already carry the whole parked population.
SCHEDULE_TO_DRAIN_HANDOVER = 200

# Leads per committed chunk — small enough that a killed worker loses little, large enough to amortise.
DRAIN_CHUNK = 100

# Journeys ended per committed chunk when a workflow is suspended or deleted. Larger than DRAIN_CHUNK
# because ending a journey is one write, not a graph walk, and the set shrinks with every pass.
STOP_CHUNK = 200

# Drains one sweep will start; more than a handful due in one minute is a misconfiguration, not load.
MAX_DUE_PER_SWEEP = 20

# Journeys started per minute, global and per workflow — the provider is the binding constraint, not us.
DRAIN_RATE = 60
DRAIN_BURST = 60
DRAIN_WINDOW = 60

# ── THE CLEANUP POSTURE ──────────────────────────────────────────────────────────────────────────
# Everything ends. What cannot complete is CLOSED WITH A REASON, never deleted on the spot: closing is
# the safety act (a closed row can never cause an action), deleting is only housekeeping. So every
# accumulating kind declares TWO ages here and nowhere else — DEAD_AFTER (short: how long the thing
# could legitimately still matter) and RETENTION (long: the audit trail outlives the behaviour).
# The test that it is right: a row past its dead-age can still be READ, and nothing it does can reach a
# patient. A literal re-stated at a call site is a second brain and `test_declared_thresholds` fails it.

# W4.4 — age at which a Pending inbox row is declared dead. Must outlast the longest arrival-to-park gap,
# because early delivery is a guarantee this engine makes. The age the code always treated as dead.
SIGNAL_DEAD_AFTER_DAYS = 30

# How long a terminal row is kept before deletion — long enough to answer "why did this never wake".
SIGNAL_RETENTION_DAYS = 7

# A journey stopped/done/failed is terminal and inert; the row is kept because "what happened to this
# patient" is asked long after. No dead-age: a journey has no waiting state that a reaper must close.
RUN_RETENTION_DAYS = 90

# A media row whose call is DELETED dies with the call, not with an age — the call is its only reason to
# exist. This is the age for the other shape: a row nothing ever resolved, whose producer never spoke
# again. `Awaiting` past it is `Abandoned` (terminal, inert, still readable), which is what the retry
# ladder already means by spending its last attempt — the age is the backstop for a row the ladder never
# reached at all, because its next attempt was never stamped.
MEDIA_DEAD_AFTER_DAYS = 7

# Terminal media rows outlive the recording debate they record, and no longer.
MEDIA_RETENTION_DAYS = 90
