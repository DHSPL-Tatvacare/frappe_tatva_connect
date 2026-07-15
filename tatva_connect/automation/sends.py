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
_RECORD_SAVEPOINT = "automation_whatsapp_record"


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
		template=template_name,
		parameters=parameters,
		lead=subject_lead,
	)


def _deliver_whatsapp(account_name, to_number, template_name, template, parameters, lead):
	"""The deferred delivery `send_whatsapp` enqueues (R1). Runs inside the background job
	`enqueue_after_commit=True` schedules - after the rule's segment has actually committed, never
	before, so a rolled-back segment (nothing was ever enqueued) never reaches this function at all.
	Re-loads the account fresh in the job's own context and calls the SAME adapter the manual/
	notification send paths use (`providers.adapter_for` -> `send_template_message`). A WATI failure
	`frappe.throw`s here, inside the job - captured by the native job runner / Error Log, never by the
	caller's transaction.

	`template_name` is WATI's name for the template (what goes on the wire); `template` is the
	`WhatsApp Templates` docname (what the row links to) - they differ, so both are queued.

	On success it records the send as a `WhatsApp Message` on the lead, carrying the WATI message id -
	the same thing the manual (`message.py`) and notification (`notification.py`) paths already do.
	Without it the CRM's only copy of an automated send was whatever WATI echoed back through the
	webhook, so a webhook that was down or a number that did not route left the patient messaged and
	the record empty. Writing the id here also gives the echo something to dedup against: WATI's
	`templateMessageSent_v2` then matches `localMessageId` and becomes a status update
	(`adapter._ingest_outbound` -> `_update_status`) instead of a duplicate Manual bubble."""
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

	# The message is on the wire: nothing below may raise — execute_job re-runs this whole function, WATI call included, on frappe.db.InternalError (deadlock/lock-wait), which is a second message to a patient.
	try:
		frappe.db.savepoint(_RECORD_SAVEPOINT)
		_record_sent_message(account_name, to_number, template, parameters, result.message_id, lead)
	except Exception:
		try:
			frappe.db.rollback(save_point=_RECORD_SAVEPOINT)
			frappe.log_error(
				title="automation: WhatsApp sent but not recorded",
				message=f"lead={lead} account={account_name} template={template} message_id={result.message_id}",
			)
		except Exception:
			pass  # log_error is itself a DB insert and can deadlock the same way — an escape here re-opens the hole it reports


def _record_sent_message(account_name, to_number, template, parameters, message_id, lead):
	"""File the sent template on the lead. The row can NEVER re-send, by four independent guards:
	`flags.tatva_ingested` short-circuits `WATIMessage.send_outgoing` before any adapter call (the only
	send seam an insert reaches); `send_outgoing` is called from `before_insert` and from the BULK
	retry alone, and the bulk retry filters on `bulk_message_reference` + `status == "Failed"`, neither
	of which this row carries; and a Template row whose `message_id` is set is skipped even if
	`send_outgoing` were reached with the in-memory flag gone (`if not self.message_id`). The DB's
	`message_id_reference_unique` (message_id, reference_name) is the last backstop."""
	if message_id and frappe.db.exists("WhatsApp Message", {"message_id": message_id, "reference_name": lead}):
		return
	doc = frappe.get_doc({
		"doctype": "WhatsApp Message",
		"type": "Outgoing",
		"message_type": "Template",
		"use_template": 1,
		"template": template,
		"template_parameters": frappe.as_json([p["value"] for p in parameters]) if parameters else None,
		"message": frappe.db.get_value("WhatsApp Templates", template, "template") or "",
		"content_type": "text",
		"to": to_number,
		"message_id": message_id,
		"status": "sent",  # never "Failed"/"Queued" — those are the states the bulk retry re-sends
		"whatsapp_account": account_name,
		"reference_doctype": "CRM Lead",
		"reference_name": lead,
	})
	doc.flags.tatva_ingested = True  # already on the wire — the controller must not send it a second time
	doc.insert(ignore_permissions=True)  # authz-ok: tier-b — background job, no user context; the send was already gated by routing + adapter.assert_enabled


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
