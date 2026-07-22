# TEMPORARY — migration reconciliation demo, remove before prod. See REMOVE-ME.md
"""The audit trail: one Integration Request per unit of work, Error Log for failures.

GRANULARITY IS THE WHOLE POINT. A 100-lead batch makes 400 LeadSquared calls; a row each would
bury the log and tell a reader nothing. One row per unit of work — a lookup, a totals run, a batch
run — carrying who asked, what was asked, how many calls it cost and what came back, is an audit
trail someone can actually read.

Integration Request is Frappe's own outbound-call log, so this needs no doctype of its own and
shows up where an administrator already looks.
"""

import json

import frappe
from frappe.integrations.utils import create_request_log

SERVICE = "Migration Check"


def log_call(
	action: str,
	request: dict,
	output: dict | None = None,
	error: str | None = None,
	calls: int = 0,
) -> str | None:
	"""Record one unit of work. Never raises — an audit failure must not fail the work."""
	try:
		payload = dict(request)
		payload.update({"action": action, "user": frappe.session.user, "lsq_calls": calls})

		# Status must go in at creation: create_request_log commits before returning, and a GET
		# request does not commit again, so a later db_set would be lost.
		doc = create_request_log(
			payload,
			service_name=SERVICE,
			request_description=action,
			is_remote_request=1,
			error=error,
			output=json.dumps(output, default=str)[:5000] if output else None,
			status="Failed" if error else "Completed",
		)
		return doc.name
	except Exception:
		# The trail is a nice-to-have; losing it must never cost the operator their answer.
		frappe.log_error(title="Migration Check — audit", message=frappe.get_traceback())
		return None


def log_error(context: str) -> None:
	"""Full traceback to Error Log, under a title that groups this tool's failures together."""
	frappe.log_error(title=f"Migration Check — {context}", message=frappe.get_traceback())
