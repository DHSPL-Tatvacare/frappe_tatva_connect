"""The voice channel's master kill-switch — one read, dormant by default.

Separate from `Workflow::Engine::sends` on purpose: that switch governs whether the ENGINE sends at all,
this one governs whether the voice channel is live in either direction. Off, a placed call's outcome is
still raw-logged by the spine (Cancelled, replayable) rather than destroyed — a switch that is off means
"do not act on this", never "throw it away".
"""
from tatva_connect.automation import settings

# An unknown key reads False, so the channel is dormant until an operator creates and ticks the row.
SWITCH_CALLS = "AI Voice::Channel::calls"

# The dormant catch-up poll (`voice.reconcile`). Off, the reconciler is a function nothing calls.
SWITCH_RECONCILE = "AI Voice::Channel::reconcile"

# Whether an author's "skip the calling window" tick is honoured at all. Off, the tick is ignored.
SWITCH_BYPASS_GUARDRAILS = "AI Voice::Channel::bypass-guardrails"


def is_enabled() -> bool:
	"""The voice master kill-switch. Dormant by default — OFF until explicitly enabled."""
	return settings.is_enabled(SWITCH_CALLS)


def reconciler_enabled() -> bool:
	"""The catch-up poll's own switch, independent of the channel's."""
	return settings.is_enabled(SWITCH_RECONCILE)


def bypass_guardrails_enabled() -> bool:
	"""Whether an author's bypass tick is honoured. Dormant by default, and the SECOND of two acts.

	Skipping the agent's calling hours takes an operator arming this row AND an author ticking the node's
	own field: a lone tick would mean one mis-click rings a patient at night. The switch is the half that
	is revocable in one click without a deploy (I8), which is why frappe's `developer_mode` is not used
	for it — that flag answers whether files may be written, and is not runtime-reversible.
	"""
	return settings.is_enabled(SWITCH_BYPASS_GUARDRAILS)
