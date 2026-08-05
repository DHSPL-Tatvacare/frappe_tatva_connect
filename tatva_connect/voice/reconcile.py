"""Catch-up poll for voice calls whose terminal webhook never arrived. DORMANT — nothing calls this.

WHY IT EXISTS. The webhook is the fast path and it is not a guarantee: a provider can drop a callback, a
deploy can eat one, and the journey parked behind it then waits for an event that will never come. The evals
platform learned this and answered it with a poller, and this is that poller ported to our shapes —
correctness never depends on a callback arriving.

WHY IT IS OFF. It is gated on `AI Voice::Channel::reconcile`, a switch whose row does not exist, so
`settings.is_enabled` reads False and every entry point below returns immediately. Two lines still have to
be written by hand before it can ever run, and they are deliberately NOT written here (both files are
being edited by another change):

    hooks.py            scheduler_events["cron"]["*/15 * * * *"] += ["tatva_connect.voice.reconcile.sweep"]
    automation/registry.py   Auto("AI Voice::Channel::reconcile", "AI Voice — catch up on call outcomes", ...)

Until both exist this module is a function nothing schedules and no operator can enable.

WHAT IT DOES NOT DO. It does not classify — `bolna.classify_outcome` is the one classifier and this asks
it. It does not wake journeys by a second route — it builds the provider's own execution payload and hands it
to `bolna.handle`, the same function the webhook worker calls, so a reconciled outcome and a delivered one
are the same event by construction. It never places a call.
"""
import frappe
from frappe.utils import add_to_date, now_datetime

from tatva_connect.automation import origin
from tatva_connect.voice import channel
from tatva_connect.workflow_engine import registry, versions

# How long a call is given to report itself before the poll goes looking. Comfortably past any real call.
GRACE_MINUTES = 30

# One sweep's bound. A backlog is drained over several passes rather than in one long job.
BATCH = 50

JOURNEY_DT = "CRM Workflow Journey"
JOURNEY_CALL_DT = "CRM Call Log"
# The AI Voice Call verb's own parameter for the account it places on (`actions.VERBS`).
ACCOUNT_FIELD = "connection"

_DETAIL_PREFIX = "execution_id="


def sweep():
	"""Poll every journey parked on a voice outcome past the grace window. The scheduler entry point.

	Dormant: with the switch off this is the whole function. Nothing is read, nothing is polled.
	"""
	if not channel.reconciler_enabled():
		return 0
	reconciled = 0
	for journey in _stale_parked_journeys():
		if reconcile_journey(journey.name, journey.awaiting_correlation):
			reconciled += 1
	return reconciled


def _stale_parked_journeys():
	"""Journeys parked on a voice event, untouched since the grace window closed.

	`awaiting_signal` is the flavour column `_park` writes, so this finds exactly the journeys a voice callback
	would have woken — never a journey parked on a timer or on some other channel's event.
	"""
	return frappe.get_all(  # authz-ok: tier-a — workflow engine, scheduler context
		JOURNEY_DT,
		filters={
			"status": "Parked",
			"awaiting_signal": ["like", "voice.%"],
			"awaiting_correlation": ["is", "set"],
			"modified": ["<", add_to_date(now_datetime(), minutes=-GRACE_MINUTES)],
		},
		fields=["name", "awaiting_correlation"],
		limit=BATCH,
		order_by="modified asc",
	)


def reconcile_journey(journey, correlation):
	"""Ask the provider what became of this journey's call, and deliver the outcome if it is terminal.

	Returns True when an outcome was delivered. A call still in flight, an execution we cannot identify,
	or a provider that will not answer all leave the journey exactly as it was — a poll that cannot tell must
	never invent an outcome, because the outcome it invents routes a real patient down a real branch.
	"""
	if not channel.reconciler_enabled():
		return False
	execution_id, account = _placement_of(journey, correlation)
	if not (execution_id and account):
		return False

	from tatva_connect.voice import api as voice_api

	adapter = voice_api.adapter_for(account)
	try:
		execution = adapter.fetch_execution(voice_api.connection_for(account), execution_id)
	except Exception:
		frappe.log_error(title="voice reconcile: execution fetch failed",
		                 message=f"journey={journey} execution_id={execution_id}\n{frappe.get_traceback()}")
		return False

	if not adapter.is_terminal(execution.get("status")):
		return False

	# The provider's payload carries no `user_data` on this route, so the token this journey is parked on is
	# put back where the callback would have echoed it. Same shape in, same `handle`, same wake.
	execution = {**execution, "user_data": {adapter.USER_DATA_CORRELATION_KEY: correlation}}
	if adapter.already_processed(execution, None, account):
		return False
	adapter.handle(execution, None, account)
	return True


def _placement_of(journey, correlation):
	"""(execution_id, account) for the node this journey is parked behind, each read from what already holds it.

	THE EXECUTION ID *IS* THE CALL ROW. `CRM Call Log` autonames from the provider's id, and the engine
	stamps the journey's token onto that row through the one map in `origin.AUTOMATION_STAMP` — so finding
	it is a single indexed seek and there is no audit row to keep in step with anything.

	The account comes off the journey's FROZEN node config, which is authoritative for a different reason:
	it is the configuration this call was actually placed on, and a later edit to the workflow cannot move it.
	"""
	stamp = origin.AUTOMATION_STAMP[JOURNEY_CALL_DT]
	call = frappe.db.get_value(JOURNEY_CALL_DT, {stamp: correlation}, "name")  # authz-ok: tier-a — workflow engine, scheduler context
	return (call or None), (_account_of(journey, correlation) if call else None)


def _account_of(journey, correlation):
	"""The voice account this node declared, off the version the journey is bound to."""
	_run, _, node_id = (correlation or "").partition("::")
	version = frappe.db.get_value(JOURNEY_DT, journey, "workflow_version")
	if not (node_id and version):
		return None
	node = next((n for n in versions.load(version).nodes if n.node_id == node_id), None)
	return registry.config_of(node).get(ACCOUNT_FIELD) if node else None
