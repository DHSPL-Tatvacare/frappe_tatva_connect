"""The dormant sends gate (Task 7) — every outbound WhatsApp/email the automation engine's `Send
WhatsApp` / `Send Email` effect verbs raise goes through here. `Task::Automation::sends` ships OFF
(A.6, dormant by default — a blank/absent row reads as disabled): while dormant, both functions
record a `"suppressed: sends dormant"` marker and call NO adapter — the engine runs end to end (rule
fires, action runs, Run Log records it) without a message ever leaving. Once the operator flips the
switch, the SAME functions resolve against the EXISTING WhatsApp send brain (`whatsapp.routing`
grain-routing + `channels.resolve.adapter_for` + the adapter's `send_template`) or native
`frappe.sendmail` (A.18), never a second HTTP path (A.11/A.8, one brain). Both irreversible side
effects RIDE the rule's own segment transaction (R1, post-audit remediation): Send WhatsApp defers
its provider call past commit via a thunk (`_deliver_whatsapp`), Send Email drops `now=True` so the Email
Queue insert is itself the transactional write - a segment that rolls back sends nothing either way.

DATA IS ROUTED, AUTHOR ERROR RAISES — the split, and why it falls where it does
------------------------------------------------------------------------------
Both verbs declare two outputs, `sent` and `failed`, and every function here returns which one the run
leaves by. Before this, neither verb declared any output at all, so every failure mode RAISED, reached
`interpreter.advance`, and marked the whole run **Failed**. "This patient has no phone number" is an
ordinary state of a patient record, not a system fault, and killing a journey over it is wrong — the
author must be able to route it, which is what a real production flow (WhatsApp → wait → call → branch)
wires on every messaging node.

The line is drawn at WHO CAN FIX IT, not at how bad it is:

  * `failed` — anything a PATIENT'S DATA or the ENVIRONMENT can produce, where the same workflow is still
    correct for the next record. No `mobile_no`; no WhatsApp routing for this lead's grain; the account or
    channel switched off; a declared mapping row whose variable resolved blank; a recipient variable that
    resolved to nothing. All of these mean exactly one thing — the message did not reach the patient — and
    that is precisely what the author's `failed` edge is for. Raising instead turns a routable, per-record
    condition into a dead run, and during a channel outage it would kill every run on the site rather than
    let each one take its declared failure path.
  * RAISE — anything only the AUTHOR can fix, which no data state can produce and which is wrong for every
    record equally. A blank template or recipient or subject or body on the node; a template that does not
    belong to the account its grain resolves to; a template placeholder with no mapping row declared for
    it. Routing these to `failed` would hide a broken workflow behind an edge that says "the patient did
    not get it", and the author would never learn the message could never have been sent at all.

A template/account mismatch is deliberately on the RAISE side even though the account is grain-resolved:
the pairing is a static fact about two operator-owned records, not about the patient, and a mismatch means
the workflow is systematically wrong for a whole grain.

THERE IS A THIRD CATEGORY, AND IT IS NEITHER OF THOSE — TRANSIENT
-----------------------------------------------------------------
WATI answers **HTTP 429** when a rate limit is exceeded (the account is on the Pro plan: 60 requests per
10s on `/sendTemplateMessage`), and 5xx on its own server faults. A 429 is not DATA and not author error;
it means "ask again shortly", and BOTH obvious placements are wrong. Routing it to `failed` would drop
that patient's message for a reason that has nothing to do with the patient — on a scheduled campaign,
silently dropping a slice of the cohort down the failure branch. Raising and marking the run Failed is
worse: a healthy journey killed because the sender was momentarily busy. **Do not "simplify" a transient
into the failure edge.** It is a third thing, and it needs retry, not routing.

Today no transient can reach the routing decision, BY CONSTRUCTION, and that is why this phase builds no
retry: `send_whatsapp` returns before any provider call happens. Everything it judges is local — the
lead's own record, grain routing, the switches, the declared mapping — and the HTTP request is made later,
in `_deliver_whatsapp`, inside the background job `enqueue_after_commit=True` schedules. So `SENT` here
means "accepted for delivery and handed to the provider", never "the patient received it"; a 429 lands in
the job, where `_deliver_whatsapp` raises and the native job runner's failed registry holds it. That is
the existing retry classification doing its job, the status is not swallowed, and nothing here marks the
run terminal on a transient. Real backoff against the token bucket is Phase 5 work.

WHICH EDGE A DORMANT SEND LEAVES BY: `sent`
-------------------------------------------
`sent`, and this is not a detail. If a suppressed send left by `failed`, then on every bench where the
switch ships OFF — which is every bench, by A.6 — a journey would walk its "could not reach the patient"
branch: escalation tasks raised, retries fired, alerts sent, for patients nothing was ever attempted for.
The dormant switch would change the SHAPE of the workflow rather than merely stopping a message, which is
the opposite of what dormant-by-default means. Nothing failed; a send was suppressed by policy. The
marker string in the step log is what proves it was suppressed, and the graph runs end to end as authored.

The DATA checks above still run BEFORE the gate, so a dormant bench routes a lead with no `mobile_no` to
`failed` exactly as a live one would — the switch suppresses the message, never the truth about the record.
"""
import frappe

from tatva_connect import automation

SENDS_SWITCH = "Task::Automation::sends"
_RECORD_SAVEPOINT = "automation_whatsapp_record"

# The two edges a send leaves by. Declared here, beside the code that CHOOSES between them, and read by
# `actions.VERBS` — so the names the canvas draws and the names the sender returns cannot drift apart.
SENT = "sent"
FAILED = "failed"

DORMANT_MARKER = "suppressed: sends dormant"


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


def send_whatsapp(subject_lead, template_name, context=None, values=None):
	"""Validate and resolve a template send to `subject_lead`'s `mobile_no`, then RETURN
	`(output, deferred-thunk-or-marker)` instead of sending inline (R1, post-audit remediation). Every
	check that can fail the segment (blank config, no routing, a disabled account, a template/account
	mismatch) runs here, SYNCHRONOUSLY, so a bad rule still fails before anything is queued. Only the
	actual provider HTTP call moves out: the returned `lambda: frappe.enqueue(...,
	enqueue_after_commit=True, ...)` mirrors `actions._action_call_webhook` (A.8) -
	`dispatcher.run_effects` appends it to `deferred` and runs it only after the segment's savepoint is
	released, and `enqueue_after_commit=True` fires the job only once the outer transaction actually
	commits. A rolled-back segment (a later sibling action fails) therefore enqueues nothing - no
	double-send on retry, no message reaching a patient the audit trail claims never went out.

	`output` is `SENT` or `FAILED` per the split in this module's docstring; the caller puts it into
	`_output`, which `interpreter._verb_output` validates against the verb's DECLARED outputs. While
	dormant the answer is `(SENT, marker)` - nothing to defer, nothing was queued.

	`values` is the node's DECLARED template mapping, and it is the whole reason a placeholder is no
	longer an invisible read of run state. `_template_parameters` builds the outbound list from it - this
	stays the one and only place outbound WhatsApp parameters are assembled."""
	if not template_name:
		raise ValueError("Send WhatsApp action has no WhatsApp Template configured")
	lead = frappe.get_doc("CRM Lead", subject_lead)
	recipient = lead.get("mobile_no")
	if not recipient:
		return FAILED, f"failed: lead {subject_lead} has no mobile_no to send to"

	if not sends_enabled():
		return SENT, DORMANT_MARKER

	from tatva_connect.channels import resolve
	from tatva_connect.whatsapp import channel, routing

	account_name = routing.resolve_account_for_lead(lead)
	if not account_name:
		return FAILED, f"failed: no WhatsApp routing for lead {subject_lead}'s grain"
	account = frappe.get_doc("WhatsApp Account", account_name)
	if not channel.is_enabled():
		return FAILED, "failed: the WhatsApp channel is switched off"
	adapter = resolve.adapter_for(account)
	template = frappe.get_doc("WhatsApp Templates", template_name)
	mismatch = template_account_mismatch(template_name, account_name)
	if mismatch:
		raise ValueError(f"Send WhatsApp: lead {subject_lead} - {mismatch}")

	parameters, blank = _template_parameters(adapter, account, template, values, context if context is not None else {})
	if blank:
		return FAILED, "failed: {} resolved to nothing, so the message would have gone out with a blank in it".format(
			", ".join(sorted(blank))
		)
	return SENT, lambda: frappe.enqueue(
		"tatva_connect.automation.sends._deliver_whatsapp",
		enqueue_after_commit=True,
		account_name=account_name,
		to_number=channel.normalize_number(recipient),
		template=template_name,
		parameters=parameters,
		lead=subject_lead,
	)


def _template_parameters(adapter, account, template, values, ctx):
	"""Build the outbound parameter list from the node's DECLARED mapping. Returns `(parameters, blank)`.

	THE DEFECT THIS REPLACES. This function used to be one line - `ctx.get(name)` for every placeholder
	the provider declared - and that made a WhatsApp template an undeclared read surface. The node's only
	declared param was a Link to the template, so `contract.reads_of` saw NOTHING, `graph`'s reference gate
	checked NOTHING, and a template carrying `{{patient_name}}` that no upstream node produces published
	green and then sent "Hi ," to a real patient, silently, with nothing in the step log.

	Now the author declares a row per placeholder - `{name, mode, value}` - the gate reads the
	`From Context` rows through `contract.value_row_keys` and refuses a mapping to something nothing
	upstream produces, and this function fills the slots from those rows and from nowhere else.

	The placeholder names still come from `adapter.template_variables`, which is the provider's own truth
	about the template, so an author picks from what really exists rather than typing a name.

	A slot with NO declared row RAISES: it is author error, it is wrong for every record equally, and no
	patient's data can produce it. A slot whose declared row RESOLVES BLANK is returned in `blank` and
	routes to `failed`: that IS a data state (this patient has no diagnosis recorded yet), and the one
	thing this whole change exists to prevent is putting a blank into a message to a patient. Neither case
	sends. An author who genuinely wants a blank writes a Literal row and gets one.
	"""
	from tatva_connect.workflow_engine import contract  # lazy: registry imports actions, which imports this module

	names = adapter.template_variables(account, template)
	declared = contract.value_rows_map(values)

	missing = [n for n in names if n not in declared]
	if missing:
		raise ValueError(
			"Send WhatsApp: template {} has no value declared for {} - every placeholder needs a row".format(
				template.name, ", ".join(missing)
			)
		)

	parameters, blank = [], []
	for name in names:
		mode, value = declared[name]
		resolved = ctx.get(value) if mode == contract.FROM_CONTEXT else value
		if resolved is None or str(resolved) == "":
			blank.append(name)
			continue
		parameters.append({"name": name, "value": str(resolved)})
	return parameters, blank


@frappe.whitelist()
def template_slots(template):
	"""The placeholder names this template really has, for the author's mapping control.

	Asked of the ADAPTER - `template_variables` is the provider's own answer and is what
	`_template_parameters` will really fill - so the author picks from the truth instead of typing a name
	that silently never matches. The account comes off the template itself, which is the same pairing
	`template_account_mismatch` judges; there is no lead at author time, so grain routing cannot answer.
	"""
	if not frappe.has_permission("CRM Workflow", "read"):
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)
	account_name = frappe.db.get_value("WhatsApp Templates", template, "whatsapp_account")
	if not account_name:
		return []

	from tatva_connect.channels import resolve

	account = frappe.get_doc("WhatsApp Account", account_name)
	return resolve.adapter_for(account).template_variables(account, frappe.get_doc("WhatsApp Templates", template))


def _deliver_whatsapp(account_name, to_number, template, parameters, lead):
	"""The deferred delivery `send_whatsapp` enqueues (R1). Runs inside the background job
	`enqueue_after_commit=True` schedules - after the rule's segment has actually committed, never
	before, so a rolled-back segment (nothing was ever enqueued) never reaches this function at all.
	Re-loads the account fresh in the job's own context and calls the SAME adapter surface the manual
	and notification send paths use (`resolve.adapter_for` -> `send_template`). A provider failure
	`frappe.throw`s here, inside the job - captured by the native job runner / Error Log, never by the
	caller's transaction.

	`template` is the `WhatsApp Templates` docname; the adapter resolves the provider-side name from it,
	so the wire name is never carried separately and the two can never disagree.

	THIS is where a TRANSIENT lives, and it deliberately still raises. WATI answers 429 when the account's
	rate limit is exceeded and 5xx on its own faults; neither is data the graph should route on and neither
	is author error. Raising here puts the job on the native failed registry with the provider's reason
	intact, which is the retry surface — the run itself is already committed and is NOT marked terminal by
	it. Do not "fix" this by returning a `failed` output: the routing decision has already been made and
	committed by then, and folding a rate limit into the failure edge would silently drop a slice of a
	scheduled cohort down the escalation branch. Real backoff is Phase 5.

	On success it records the send as a `WhatsApp Message` on the lead, carrying the correlation id -
	the same thing the manual (`message.py`) and notification (`notification.py`) paths already do.
	Without it the CRM's only copy of an automated send was whatever the provider echoed back through
	the webhook, so a webhook that was down or a number that did not route left the patient messaged and
	the record empty. Writing the id here also gives the echo something to dedup against: the provider's
	sent event then matches the correlation id and becomes a status update instead of a duplicate
	Manual bubble."""
	from tatva_connect.channels import resolve

	account = frappe.get_doc("WhatsApp Account", account_name)
	adapter = resolve.adapter_for(account)
	result = adapter.send_template(
		account,
		to_number,
		template,
		parameters,
		broadcast_name=f"automation_{frappe.scrub(template)}",
	)
	if result.unknown:
		# No answer from the wire — the template may already be on the patient's phone. A throw here puts the job on the failed registry, and a re-run re-sends the provider call. Recorded, not retried.
		frappe.log_error(
			title="automation: WhatsApp send outcome unknown",
			message=f"lead={lead} account={account_name} template={template} reason={result.error}",
		)
		return
	if not result.accepted:
		frappe.throw(f"Send WhatsApp failed for lead {lead}: {result.error or 'unknown provider error'}")

	# The message is on the wire: nothing below may raise — execute_job re-runs this whole function, the provider call included, on frappe.db.InternalError (deadlock/lock-wait), which is a second message to a patient.
	try:
		frappe.db.savepoint(_RECORD_SAVEPOINT)
		_record_sent_message(account_name, to_number, template, parameters, result.correlation_id, lead)
	except Exception:
		try:
			frappe.db.rollback(save_point=_RECORD_SAVEPOINT)
			frappe.log_error(
				title="automation: WhatsApp sent but not recorded",
				message=f"lead={lead} account={account_name} template={template} message_id={result.correlation_id}",
			)
		except Exception:  # nosec B110 — a re-raise here re-opens the deadlock log_error reports
			pass  # log_error is itself a DB insert and can deadlock the same way — an escape here re-opens the hole it reports


def _record_sent_message(account_name, to_number, template, parameters, message_id, lead):
	"""File the sent template on the lead. The row can NEVER re-send, by four independent guards:
	`flags.tatva_ingested` short-circuits the controller's `send_outgoing` before any adapter call (the only
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


def resolve_recipient(declared, context):
	"""The address to send to, from a field DECLARED `Variable, free_text` — the fix for defect 3.

	`email_recipient` is declared a Variable, `contract.reads_of` treats a bare identifier as a run-state
	reference, and `graph._reference_problems` ENFORCES that something upstream produces it. The handler
	then passed the raw string to `frappe.sendmail` as the address. So an author wiring
	`Set Variables → {"escalation_email": "asm@…"}` and naming `escalation_email` as the recipient got a
	green publish that ACTIVELY CERTIFIED the configuration, and Frappe queued mail to the literal string
	`"escalation_email"`. The declaration and the runtime disagreed, and publish sided with the wrong one.

	Resolving is the right half to keep, not dropping the declaration: an author naming an upstream value
	as the recipient is a real and necessary pattern (escalate to whoever the Call API said owns this
	patient), and it is the pattern the gate was already built to check. Dropping the declaration would
	delete a working check to match a broken runtime.

	The SAME predicate decides both sides — `contract.is_free_text_reference`. That is what makes them
	agree by construction rather than by two authors remembering the same rule. A literal address still
	works, and must: most authors type one.
	"""
	from tatva_connect.workflow_engine import contract  # lazy: registry imports actions, which imports this module

	if not contract.is_free_text_reference(declared):
		return declared
	return context.get(declared) if context is not None else None


def send_email(subject_lead, recipient, subject, body, context=None):
	"""Queue (or, while dormant, record) a plain email. Returns `(output, marker)` — see this module's
	docstring for which failures route and which raise.

	`context` resolves the recipient (see `resolve_recipient`); `email_subject`/`email_body` are plain
	literal fields today.

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

	address = resolve_recipient(recipient, context)
	if not address:
		return FAILED, f"failed: {recipient} resolved to no address for lead {subject_lead}"

	if not sends_enabled():
		return SENT, DORMANT_MARKER

	frappe.sendmail(
		recipients=[address],
		subject=subject,
		message=body,
		reference_doctype="CRM Lead",
		reference_name=subject_lead,
	)
	return SENT, f"queued: to={address}"
