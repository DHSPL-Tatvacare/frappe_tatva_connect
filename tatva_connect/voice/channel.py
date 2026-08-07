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


def is_enabled() -> bool:
	"""The voice master kill-switch. Dormant by default — OFF until explicitly enabled."""
	return settings.is_enabled(SWITCH_CALLS)


def reconciler_enabled() -> bool:
	"""The catch-up poll's own switch, independent of the channel's."""
	return settings.is_enabled(SWITCH_RECONCILE)
