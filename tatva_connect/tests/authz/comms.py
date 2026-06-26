# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The mandatory COMMS-OFF safety interlock.

Every authz test (and the Playwright runner) MUST call assert_comms_off() before doing anything.
Tests create leads, tasks, and messages; if a real comms channel were live, a test could fire a
real WhatsApp message or phone call to a real number. That is unacceptable, so the interlock
hard-fails the run when any channel is enabled.

Keys verified against tatva_connect/automation/registry.py — all three are CRM Tatva Automation
rows whose `enabled` Check gates a live send. is_enabled() is fail-closed (unknown/blank -> False).
"""
from tatva_connect.automation.settings import is_enabled

# Every channel that can reach a real person. Read-only: we never flip these, only assert.
# Audit-confirmed (2026-06-26): the two Notify::* keys are REAL FCM push channels — assigning a
# lead or inserting a task with an assignee fires an HTTP push to the rep's mobile (sender.py),
# gated by these keys, NOT by the three above. Missing them = a real push during a test.
COMMS_SWITCHES = (
	"WhatsApp::WATI::messaging",   # WATI outbound/inbound master gate
	"Telephony::Acefone::calls",   # Acefone click-to-call + logging
	"Task::Assignment::followup",  # auto-creates follow-up tasks (can trigger notifications)
	"Notify::Lead::assigned",      # FCM push to rep's mobile on lead assignment
	"Notify::Task::assigned",      # FCM push to rep's mobile on task assignment
)


class CommsLiveError(RuntimeError):
	"""Raised when a comms channel is ON — the test refuses to run rather than risk a live send."""


def comms_on():
	"""Return the list of comms switches currently enabled (empty == safe)."""
	return [key for key in COMMS_SWITCHES if is_enabled(key)]


def assert_comms_off():
	"""Hard-fail the test/run if ANY comms channel is live. Call first in every setUp."""
	live = comms_on()
	if live:
		raise CommsLiveError(
			"COMMS ARE LIVE — refusing to run authz tests (risk of a real WhatsApp/call). "
			"Disable these CRM Tatva Automation switches first: {0}".format(", ".join(live))
		)
