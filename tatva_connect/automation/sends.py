"""The dormant sends gate (Task 7) — every outbound WhatsApp/email the automation engine's `Send
WhatsApp` / `Send Email` effect verbs raise goes through here. `Task::Automation::sends` ships OFF
(A.6, dormant by default — a blank/absent row reads as disabled): while dormant, both functions
record a `"suppressed: sends dormant"` marker and call NO adapter — the engine runs end to end (rule
fires, action runs, Run Log records it) without a message ever leaving. Once the operator flips the
switch, the SAME functions resolve against the EXISTING WATI send brain (`whatsapp.routing`
grain-routing + `whatsapp.providers.adapter_for` + `whatsapp.api.send_template_message`) or native
`frappe.sendmail` (A.18), never a second HTTP path (A.11/A.8, one brain). Both irreversible side
effects RIDE the rule's own segment transaction (R1, post-audit remediation): Send WhatsApp defers
its WATI call past commit via a thunk (`_deliver_whatsapp`), Send Email drops `now=True` so the Email
Queue insert is itself the transactional write - a segment that rolls back sends nothing either way.

A blank template/recipient/subject/body is a misconfiguration, not "nothing to send" — every
validation RAISES (fail-closed) regardless of the gate, so a broken rule surfaces at fire time
instead of silently no-op'ing forever, live or dormant.
"""
import frappe

from tatva_connect import automation

SENDS_SWITCH = "Task::Automation::sends"


def sends_enabled() -> bool:
	return automation.is_enabled(SENDS_SWITCH)


def template_account_mismatch(template_name, account_name) -> str | None:
	"""Shared predicate (A.8): does the picked WhatsApp Template belong to the account it is about to
	send through? Returns None when they match, or a ready error message naming both accounts when
	they do not. Shared by the send-time guard below (send_whatsapp) and the author-time validator
	(crm_automation_rule._validate_send_whatsapp) - the comparison lives in exactly one place."""
	template_account = frappe.db.get_value("WhatsApp Templates", template_name, "whatsapp_account")
	if template_account == account_name:
		return None
	return f"template {template_name} belongs to account {template_account}, but the resolved account is {account_name}"


def send_whatsapp(subject_lead, template_name, context=None):
	"""Validate and resolve a WATI template send to `subject_lead`'s `mobile_no`, then RETURN a
	deferred thunk instead of sending inline (R1, post-audit remediation). Every check that can fail
	the segment (blank config, no routing, a disabled account, a template/account mismatch) runs here,
	SYNCHRONOUSLY, so a bad rule still fails before anything is queued. Only the actual WATI HTTP call
	moves out: the returned `lambda: frappe.enqueue(..., enqueue_after_commit=True, ...)` mirrors
	`actions._action_call_webhook` (A.8) - `dispatcher.run_effects` appends it to `deferred` and runs
	it only after the segment's savepoint is released, and `enqueue_after_commit=True` fires the job
	only once the outer transaction actually commits. A rolled-back segment (a later sibling action
	fails) therefore enqueues nothing - no double-send on retry, no message reaching a patient the
	audit trail claims never went out.

	While dormant, the gate stays an inline marker string (not a thunk) - nothing to defer, nothing was
	queued."""
	if not template_name:
		raise ValueError("Send WhatsApp action has no WhatsApp Template configured")
	lead = frappe.get_doc("CRM Lead", subject_lead)
	recipient = lead.get("mobile_no")
	if not recipient:
		raise ValueError(f"Send WhatsApp: lead {subject_lead} has no mobile_no to send to")

	if not sends_enabled():
		return "suppressed: sends dormant"

	from tatva_connect.whatsapp import providers, routing

	account_name = routing.resolve_account_for_lead(lead)
	if not account_name:
		raise ValueError(f"Send WhatsApp: no WATI routing for lead {subject_lead}'s grain")
	account = frappe.get_doc("WhatsApp Account", account_name)
	adapter = providers.adapter_for(account)
	adapter.assert_enabled()
	template = frappe.get_doc("WhatsApp Templates", template_name)
	mismatch = template_account_mismatch(template_name, account_name)
	if mismatch:
		raise ValueError(f"Send WhatsApp: lead {subject_lead} - {mismatch}")
	names = adapter.template_param_names(template)
	ctx = context or {}
	parameters = [{"name": n, "value": "" if ctx.get(n) is None else str(ctx[n])} for n in names]
	actual_name = template.actual_name or template.template_name
	return lambda: frappe.enqueue(
		"tatva_connect.automation.sends._deliver_whatsapp",
		enqueue_after_commit=True,
		account_name=account_name,
		to_number=adapter.normalize_number(recipient),
		template_name=actual_name,
		parameters=parameters,
		lead=subject_lead,
	)


def _deliver_whatsapp(account_name, to_number, template_name, parameters, lead):
	"""The deferred delivery `send_whatsapp` enqueues (R1). Runs inside the background job
	`enqueue_after_commit=True` schedules - after the rule's segment has actually committed, never
	before, so a rolled-back segment (nothing was ever enqueued) never reaches this function at all.
	Re-loads the account fresh in the job's own context and calls the SAME adapter the manual/
	notification send paths use (`providers.adapter_for` -> `send_template_message`). A WATI failure
	`frappe.throw`s here, inside the job - captured by the native job runner / Error Log, never by the
	caller's transaction."""
	from tatva_connect.whatsapp import providers

	account = frappe.get_doc("WhatsApp Account", account_name)
	adapter = providers.adapter_for(account)
	resp = adapter.send_template_message(
		account,
		to_number=to_number,
		template_name=template_name,
		broadcast_name=f"automation_{frappe.scrub(template_name)}",
		parameters=parameters,
	)
	result = adapter.classify_send_response(resp)
	if result.failed:
		frappe.throw(f"Send WhatsApp failed for lead {lead}: {result.reason or 'unknown WATI error'}")


def send_email(subject_lead, recipient, subject, body, context=None) -> str:
	"""Queue (or, while dormant, record) a plain email. `context` is accepted for parity with
	`send_whatsapp` (a future Expression-mode subject/body would resolve against it before this call)
	but is not otherwise used; `email_subject`/`email_body` are plain literal fields today.

	No `now=True` (R1, post-audit remediation): `frappe.sendmail` then only inserts an Email Queue row,
	a normal DB write that rides the rule's own segment transaction - a rollback removes the queued row
	with everything else it undid, so a later sibling action's failure means the mail never sends. This
	is the most native ride-the-transaction fix; unlike Send WhatsApp it needs no deferred thunk, the
	queue itself is the ride."""
	if not recipient:
		raise ValueError("Send Email action has no recipient configured")
	if not subject:
		raise ValueError("Send Email action has no subject")
	if not body:
		raise ValueError("Send Email action has no body")

	if not sends_enabled():
		return "suppressed: sends dormant"

	frappe.sendmail(
		recipients=[recipient],
		subject=subject,
		message=body,
		reference_doctype="CRM Lead",
		reference_name=subject_lead,
	)
	return f"queued: to={recipient}"
