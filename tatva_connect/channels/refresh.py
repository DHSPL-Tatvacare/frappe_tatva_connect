# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Re-pull ONE record's history from a provider, on a person's click — the plumbing, expressed once.

A rep looks at a lead, sees a gap, and presses Refresh. What follows is the same four things whichever
channel they pressed it on: refuse a second run for that record, queue the walk, do it, and tell the
person who asked how it went. None of those four is about WhatsApp or about calls.

WHAT A CHANNEL BRINGS is declared in `CHANNELS` below and is only ever three facts: who may ask, what to
run, and what to call it when it fails. The walk itself stays where it belongs — `backfill_lead` owns a
WhatsApp thread, `reconcile_lead` owns a call log — and neither knows it is being refreshed rather than
called directly.

TOLD TO ONE PERSON, NOT TO A ROOM. Pressing Refresh is an ACTION, and its progress concerns only whoever
pressed it, so the event goes to that user and no other browser is sent it. What the refresh WRITES is a
different thing and already has its own events to the record's room — `whatsapp_message`,
`telephony_call` — so a colleague watching the lead still sees the new rows, with none of the chatter.

RQ IS THE LOCK AND THE TRUTH. `enqueue(deduplicate=True)` on a job id built from channel and record is the
whole cross-user guard — a second click, from anyone, is not a second job. `state()` asks RQ the same
question, which is how a second rep's screen learns a refresh is already running without being told.
"""
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils.background_jobs import is_job_enqueued

# The one event every channel's refresh reports on; its payload names the channel.
EVENT = "tatva_record_refresh"

# The queue: a provider walk is seconds to minutes, and `short` is the latency lane.
QUEUE = "long"


@dataclass(frozen=True)
class Channel:
	"""One channel's three facts. Dotted paths, because the runner is also what `enqueue` is handed."""

	gate: str
	runner: str
	failed: str


CHANNELS = {
	"whatsapp": Channel(
		gate="tatva_connect.channels.refresh.gate_whatsapp",
		runner="tatva_connect.whatsapp.backfill.backfill_lead",
		failed="WhatsApp refresh failed.",
	),
	"calls": Channel(
		gate="tatva_connect.channels.refresh.gate_calls",
		runner="tatva_connect.telephony.reconcile.reconcile_lead",
		failed="Call refresh failed.",
	),
}


def gate_whatsapp(reference_doctype, reference_name):
	"""The WhatsApp capability, then the lead itself — the same pair `refresh_history` has always asked."""
	from crm.api.whatsapp import validate_access

	validate_access(reference_doctype, reference_name)
	frappe.has_permission(reference_doctype, "write", doc=reference_name, throw=True)


def gate_calls(reference_doctype, reference_name):
	"""Write access to the lead: a caller who cannot act on it may not probe its call history either."""
	frappe.has_permission(reference_doctype, "write", doc=reference_name, throw=True)


def _channel(name) -> Channel:
	"""The channel's declaration, or a refusal — never a default, because a typo would silently refresh the wrong thing."""
	channel = CHANNELS.get(name)
	if not channel:
		frappe.throw(_("{0} is not a channel that can be refreshed.").format(name))
	return channel


def job_id(channel: str, reference_name: str) -> str:
	"""The RQ job id, composed HERE and nowhere else — it is the lock, and two spellings of it would give a screen that says idle while a job runs."""
	return f"tatva-refresh:{channel}:{reference_name}"


@frappe.whitelist()
def state(channel, reference_doctype, reference_name) -> dict:
	"""Is a refresh running for this record, right now, for ANYONE — how a second rep's screen learns without being told."""
	frappe.get_attr(_channel(channel).gate)(reference_doctype, reference_name)
	return {"running": is_job_enqueued(job_id(channel, reference_name))}


@frappe.whitelist()
def start(channel, reference_doctype, reference_name) -> dict:
	"""Queue one refresh and say so. Returns immediately — the walk is somebody else's API, not ours."""
	declared = _channel(channel)
	frappe.get_attr(declared.gate)(reference_doctype, reference_name)
	if reference_doctype != "CRM Lead":
		frappe.throw(_("A refresh runs on a lead."))

	# Carried through the queue: the worker has no session, and this is the ONE person the outcome is for.
	asked_by = frappe.session.user
	queued = frappe.enqueue(
		"tatva_connect.channels.refresh.run",
		queue=QUEUE,
		job_id=job_id(channel, reference_name),
		deduplicate=True,
		channel=channel,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		asked_by=asked_by,
	)
	# `deduplicate` answers None when someone else's walk is already running, and its outcome is addressed to THEM. Saying "started" here would grey this rep's button until their client gave up on a `finished` that was never coming.
	if not queued:
		return {"queued": False}

	publish(channel, reference_doctype, reference_name, "started", asked_by)
	return {"queued": True}


def run(channel, reference_doctype, reference_name, asked_by) -> None:
	"""The queued half. `finished` comes from a finally-block: a screen told only about success stays disabled for ever the day the provider is down."""
	declared = _channel(channel)
	outcome = {"error": _(declared.failed)}
	try:
		summary = frappe.get_attr(declared.runner)(reference_name, dry_run=False)
		if not summary.get("ok"):
			outcome = {"error": summary.get("reason") or _(declared.failed)}
			return
		outcome = {"count": summary.get("new", 0), "existing": summary.get("existing", 0)}
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title=f"{channel} refresh failed", message=frappe.get_traceback())
		frappe.db.commit()
	finally:
		publish(channel, reference_doctype, reference_name, "finished", asked_by, **outcome)


def publish(channel, reference_doctype, reference_name, state, asked_by, **payload) -> None:
	"""The whole lifecycle, to the ONE person who asked — never the record's room, never the site's."""
	frappe.publish_realtime(
		EVENT,
		{
			"channel": channel,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"state": state,
			**payload,
		},
		user=asked_by,
	)
