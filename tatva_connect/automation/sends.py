"""The dormant sends gate (Task 7) — every outbound WhatsApp/email the automation engine's `Send
WhatsApp` / `Send Email` effect verbs raise goes through here. `Task::Automation::sends` ships OFF
(A.6, dormant by default — a blank/absent row reads as disabled): while dormant, both functions
record a `"suppressed: sends dormant"` marker and call NO adapter — the engine runs end to end (rule
fires, action runs, Run Log records it) without a message ever leaving. Once the operator flips the
switch, the SAME functions call the EXISTING WATI send brain (`whatsapp.routing` grain-routing +
`whatsapp.providers.adapter_for` + `whatsapp.api.send_template_message`) or native `frappe.sendmail`
(A.18) — never a second HTTP path (A.11/A.8, one brain).

A blank template/recipient/subject/body is a misconfiguration, not "nothing to send" — every
validation RAISES (fail-closed) regardless of the gate, so a broken rule surfaces at fire time
instead of silently no-op'ing forever, live or dormant.
"""
import frappe

from tatva_connect import automation

SENDS_SWITCH = "Task::Automation::sends"


def sends_enabled() -> bool:
	return automation.is_enabled(SENDS_SWITCH)


def send_whatsapp(subject_lead, template_name, context=None) -> str:
	"""Send (or, while dormant, record) a WATI template message to `subject_lead`'s `mobile_no`.

	Validates config first (fail-closed, both gate states), then — live only — resolves the
	grain-routed account (`whatsapp.routing.resolve_account_for_lead`, A.11: no global default, an
	unmatched lead's grain blocks the send) and calls the SAME adapter the manual/notification send
	paths use (`providers.adapter_for` -> `whatsapp.api.send_template_message`)."""
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
	names = adapter.template_param_names(template)
	ctx = context or {}
	parameters = [{"name": n, "value": "" if ctx.get(n) is None else str(ctx[n])} for n in names]
	resp = adapter.send_template_message(
		account,
		to_number=adapter.normalize_number(recipient),
		template_name=template.actual_name or template.template_name,
		broadcast_name=f"automation_{frappe.scrub(template.actual_name or template.template_name)}",
		parameters=parameters,
	)
	result = adapter.classify_send_response(resp)
	if result.failed:
		frappe.throw(f"Send WhatsApp failed: {result.reason or 'unknown WATI error'}")
	return f"sent: template={template_name} to={recipient}"


def send_email(subject_lead, recipient, subject, body, context=None) -> str:
	"""Send (or, while dormant, record) a plain email. `context` is accepted for parity with
	`send_whatsapp` (a future Expression-mode subject/body would resolve against it before this call)
	but is not otherwise used — `email_subject`/`email_body` are plain literal fields today."""
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
		now=True,
	)
	return f"sent: to={recipient}"
