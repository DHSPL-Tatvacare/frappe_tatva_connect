"""One request asks `frappe_whatsapp` for its notification map ONCE, not once per doc_event.

THE DEFECT IS UPSTREAM AND IT IS A MISSING READ. `frappe_whatsapp.utils.get_notifications_map`
already ends with `frappe.cache().set_value("whatsapp_notification_map", ...)` — the write is there,
the matching `get_value` never was. So the map is rebuilt from a full table scan on every call and
then written to a cache nobody reads.

WHY THAT COSTS US. That app registers `doc_events["*"]` on ELEVEN events (before_validate, validate,
before_insert, after_insert, on_update, on_trash, after_delete and the submit/cancel pair), each
routed to `run_server_script_for_doc_event`, which calls the map. Measured on this bench: 10 scans
per CRM Task insert against a table holding zero rows — about a fifth of the queries a save issues,
on every doctype, ours and frappe's alike.

WHY PER REQUEST AND NOT REDIS. A cross-request cache of another app's config would make US
responsible for busting it whenever a `WhatsApp Notification` changes — a doctype we do not own — and
a stale map means a patient message that does not send, or one that does when it should not.
`frappe.utils.caching.request_cache` is the framework's own answer: it memoises on
`frappe.local.request_cache`, which frappe clears at the end of every request and every job. Nothing
to invalidate, no window to reason about, and the clearing is the framework's job rather than ours.

WRAPPED, NOT REPLACED. The original is called on a miss, so the day that app adds the missing read —
or changes the function at all — this keeps working and simply stops mattering.
"""
from frappe.utils.caching import request_cache

_original = None


def install(*_args, **_kwargs):
	"""before_request / before_job: wrap the map reader once per process. No-op without the app."""
	global _original

	if _original is not None:
		return
	try:
		from frappe_whatsapp import utils
	except ImportError:
		return

	_original = utils.get_notifications_map
	utils.get_notifications_map = request_cache(_original)
