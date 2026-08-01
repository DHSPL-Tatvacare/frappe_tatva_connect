"""The dormant sends gate (Task 7) — every outbound WhatsApp/email the automation engine's `Send
WhatsApp` / `Send Email` effect verbs raise goes through here. `Workflow::Engine::sends` ships OFF
(A.6, dormant by default — a blank/absent row reads as disabled): while dormant, both functions
record a `"suppressed: sends dormant"` marker and call NO adapter — the engine runs end to end (rule
fires, action runs, Journey Log records it) without a message ever leaving. Once the operator flips the
switch, the SAME functions resolve against the EXISTING WhatsApp send brain (`whatsapp.routing`
grain-routing + `channels.resolve.adapter_for` + the adapter's `send_template`) or native
`frappe.sendmail` (A.18), never a second HTTP path (A.11/A.8, one brain). Both irreversible side
effects RIDE the rule's own segment transaction (R1, post-audit remediation): Send WhatsApp defers
its provider call past commit via a thunk (`_deliver_whatsapp`), Send Email drops `now=True` so the Email
Queue insert is itself the transactional write - a segment that rolls back sends nothing either way.

DATA IS ROUTED, AUTHOR ERROR RAISES — the split, and why it falls where it does
------------------------------------------------------------------------------
Both verbs declare two outputs, `sent` and `failed`, and every function here returns which one the journey
leaves by. Before this, neither verb declared any output at all, so every failure mode RAISED, reached
`interpreter.advance`, and marked the whole journey **Failed**. "This patient has no phone number" is an
ordinary state of a patient record, not a system fault, and killing a journey over it is wrong — the
author must be able to route it, which is what a real production flow (WhatsApp → wait → call → branch)
wires on every messaging node.

The line is drawn at WHO CAN FIX IT, not at how bad it is:

  * `failed` — anything a PATIENT'S DATA or the ENVIRONMENT can produce, where the same workflow is still
    correct for the next record. No `mobile_no`; no WhatsApp routing for this lead's grain; the account or
    channel switched off; a declared mapping row whose variable resolved blank; a recipient variable that
    resolved to nothing. All of these mean exactly one thing — the message did not reach the patient — and
    that is precisely what the author's `failed` edge is for. Raising instead turns a routable, per-record
    condition into a dead journey, and during a channel outage it would kill every journey on the site rather than
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
silently dropping a slice of the cohort down the failure branch. Raising and marking the journey Failed is
worse: a healthy journey killed because the sender was momentarily busy. **Do not "simplify" a transient
into the failure edge.** It is a third thing, and it needs retry, not routing.

Today no transient can reach the routing decision, BY CONSTRUCTION, and that is why this phase builds no
retry: `send_whatsapp` returns before any provider call happens. Everything it judges is local — the
lead's own record, grain routing, the switches, the declared mapping — and the HTTP request is made later,
in `_deliver_whatsapp`, inside the background job `enqueue_after_commit=True` schedules. So `SENT` here
means "accepted for delivery and handed to the provider", never "the patient received it"; a 429 lands in
the job, where `_deliver_whatsapp` raises and the native job runner's failed registry holds it. That is
the existing retry classification doing its job, the status is not swallowed, and nothing here marks the
journey terminal on a transient. Real backoff against the token bucket is Phase 5 work.

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

SENDS_SWITCH = "Workflow::Engine::sends"
_RECORD_SAVEPOINT = "automation_whatsapp_record"

# The two edges a send leaves by. Declared here, beside the code that CHOOSES between them, and read by
# `actions.VERBS` — so the names the canvas draws and the names the sender returns cannot drift apart.
SENT = "sent"
FAILED = "failed"

# W12 — the channel words the step log records. One vocabulary, used by the logger and by the cap that
# counts mobile channels; `email` is recorded and deliberately NOT counted (a cap on a number cannot
# have an address folded into it).
WHATSAPP, VOICE, EMAIL = "whatsapp", "voice", "email"
MOBILE_CHANNELS = (WHATSAPP, VOICE)

# Voice's synchronous "handed to the provider for dialling" output — never "answered", which is a later
# channel-reported outcome. Named beside SENT so the messaging and voice send verbs read one vocabulary.
PLACED = "placed"

DORMANT_MARKER = "suppressed: sends dormant"


def _canonical_contact(number):
	"""W12 — the identity this number is COUNTED and LOGGED under, or blank. Never the conformed form.

	`to_e164` is the ONE store brain (`phone.py`'s three jobs), and it is deliberately not
	`Declaration.conform_number`: WATI is handed `91…` and Bolna `+91…`, so logging what actually went on
	the wire would write one patient as two rows and the cap would count them as two people and silently
	never fire.

	A refusal comes back BLANK, and that differs on purpose from `api._base._norm_phone`, which passes the
	unshapeable value through because it is building a search filter that should simply match nothing.
	Here the value becomes an IDENTITY, so a raw string would be a second identity for the same person —
	the exact defect above. A number that is not real anywhere therefore has no cap identity, which is the
	ruled behaviour: such a lead cannot be messaged today either, so it never reaches the cap.
	"""
	from tatva_connect.whatsapp.phone import to_e164

	try:
		return to_e164(number)
	except frappe.ValidationError:
		frappe.clear_last_message()  # the refusal is not this caller's error to surface
		return ""


def _record_contact(context, channel, address):
	"""Tell the interpreter WHO this verb reached and HOW, through the engine namespace `_engine.output`
	already travels on — the verb writes, the interpreter pops, and no signature carries it.

	Recorded as soon as the address resolves, so a suppressed or refused step still says who it was for:
	"which number did this journey message?" is asked of the failures more often than of the successes.
	"""
	from tatva_connect.workflow_engine import refs

	if context is None:
		return
	context[refs.CHANNEL] = channel
	context[refs.CONTACT] = _canonical_contact(address) if channel in MOBILE_CHANNELS else (address or "")


def sends_enabled() -> bool:
	return automation.is_enabled(SENDS_SWITCH)


def missing_value_rows(names, values) -> list:
	"""Placeholder/slot names with NO declared mapping row — the ONE detector, shared by the publish gate
	(`graph._template_mapping_problems`) and both send-time builders below. A name with no row is author
	error: no patient's data can produce it and it is wrong for every record equally. Publish now refuses
	it; the builders keep it as a backstop because deleting the guard would `KeyError` on the fill and risk
	assembling a message with a slot silently dropped — the exact hole this whole change exists to close."""
	from tatva_connect.workflow_engine import contract

	declared = contract.value_rows_map(values)
	return [n for n in names if n not in declared]


def whatsapp_template_slots(template) -> list:
	"""The placeholder names a WhatsApp template really declares, from the provider's own truth (the
	adapter) for the account the template belongs to. The non-whitelisted core the whitelisted
	`template_slots` wraps AND the publish gate reads — so author-time refusal and send-time fill see the
	SAME names, never two lists that can drift."""
	account_name = frappe.db.get_value("WhatsApp Templates", template, "whatsapp_account")
	if not account_name:
		return []
	from tatva_connect.channels import resolve

	account = frappe.get_doc("WhatsApp Account", account_name)
	return resolve.adapter_for(account).template_variables(account, frappe.get_doc("WhatsApp Templates", template))


def template_account_mismatch(template_name, account_name) -> str | None:
	"""Shared predicate (A.8): does the picked WhatsApp Template belong to the account it is about to
	send through? Returns None when they match, or a ready error message naming both accounts when
	they do not. Shared by the send-time guard below (send_whatsapp) and the author-time validator
	(crm_automation_rule._validate_send_whatsapp) - the comparison lives in exactly one place."""
	template_account = frappe.db.get_value("WhatsApp Templates", template_name, "whatsapp_account")
	if template_account == account_name:
		return None
	return f"template {template_name} belongs to account {template_account}, but the resolved account is {account_name}"


def send_whatsapp(subject_lead, contact_number, template_name, context=None, values=None, correlation=None):
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

	`contact_number` is the REF the node declared, resolved here against journey state. The lead's `mobile_no` is no longer read
	here and there is no fallback to it: the author picks the contact field, and a recipient that resolves
	to nothing is a routable refusal rather than a quiet substitution. Nobody choosing the recipient is
	what put a real patient's message on a stranger's phone in another country.

	`correlation` is the engine's own token for the node that is sending (`journey::node`). It is carried
	all the way to the `WhatsApp Message` row, which is what lets a delivery receipt arriving seconds
	later wake THIS journey rather than another journey parked on the same lead. The provider's id does not exist
	yet at this point - the journey parks before the send job runs - so the token is what travels, and the row
	is where the two identities finally meet.

	`values` is the node's DECLARED template mapping, and it is the whole reason a placeholder is no
	longer an invisible read of journey state. `_template_parameters` builds the outbound list from it - this
	stays the one and only place outbound WhatsApp parameters are assembled."""
	if not template_name:
		raise ValueError("Send WhatsApp action has no WhatsApp Template configured")
	lead = frappe.get_doc("CRM Lead", subject_lead)
	# A pure REFERENCE read. `resolve_recipient` is deliberately not used: it carries a literal path for Send Email's typed addresses, and a phone number must never be typed into a node.
	recipient = (context or {}).get(contact_number) if contact_number else None
	if not recipient:
		return FAILED, f"failed: {contact_number or 'no contact number'} resolved to no number for lead {subject_lead}"
	_record_contact(context, WHATSAPP, recipient)

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

	# The one gate, after the author-error checks above so a broken node stays loud whatever this patient's data looks like.
	to_number, refusal = channel.screen_send(adapter, recipient, "CRM Lead", subject_lead)
	if refusal:
		return FAILED, f"failed: {refusal}"

	parameters, blank = _template_parameters(adapter, account, template, values, context if context is not None else {})
	if blank:
		return FAILED, "failed: {} resolved to nothing, so the message would have gone out with a blank in it".format(
			", ".join(sorted(blank))
		)
	return SENT, lambda: frappe.enqueue(
		"tatva_connect.automation.sends._deliver_whatsapp",
		# Named, not defaulted: `default` is consumed by BOTH worker services, so an unnamed send rides the same lane as the sweep that rescues parked journeys.
		queue="workflow",
		enqueue_after_commit=True,
		account_name=account_name,
		to_number=to_number,
		template=template_name,
		parameters=parameters,
		lead=subject_lead,
		correlation=correlation,
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
	from tatva_connect.workflow_engine import (
		contract,  # lazy: registry imports actions, which imports this module
	)

	names = adapter.template_variables(account, template)
	declared = contract.value_rows_map(values)

	missing = missing_value_rows(names, values)
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
	return whatsapp_template_slots(template)


def _deliver_whatsapp(account_name, to_number, template, parameters, lead, correlation=None):
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
	intact, which is the retry surface — the journey itself is already committed and is NOT marked terminal by
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
		_record_sent_message(account_name, to_number, template, parameters, result.correlation_id, lead, correlation, result.wamid)
	except Exception:
		try:
			frappe.db.rollback(save_point=_RECORD_SAVEPOINT)
			frappe.log_error(
				title="automation: WhatsApp sent but not recorded",
				message=f"lead={lead} account={account_name} template={template} message_id={result.correlation_id}",
			)
		except Exception:  # nosec B110 — a re-raise here re-opens the deadlock log_error reports
			pass  # log_error is itself a DB insert and can deadlock the same way — an escape here re-opens the hole it reports


def _record_sent_message(account_name, to_number, template, parameters, message_id, lead, correlation=None, wamid=None):
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
		# The two identities meet HERE and nowhere else: the provider's id and the journey+node that sent it.
		"custom_workflow_correlation": correlation,
		# The tap-join key. `message_id` above stays the localMessageId, which is the only id status events echo.
		"custom_outbound_wamid": wamid,
	})
	doc.flags.tatva_ingested = True  # already on the wire — the controller must not send it a second time
	doc.insert(ignore_permissions=True)  # authz-ok: tier-b — background job, no user context; the send was already gated by routing + adapter.assert_enabled


def _preview_context(lead=None):
	"""The lead an author's preview is built from, and the journey-shaped context over it. Returns `(doc, ctx)`.

	The SAME choice `context.test_call` makes for Call API: pick the most recently touched lead unless the
	author named one, and say which. A response — or a message — is shaped by the record behind it, and an
	author reading one built from a lead they did not choose maps values that do not exist for the next.
	"""
	from tatva_connect.automation.context import context_for

	subject = lead or frappe.db.get_value("CRM Lead", {}, "name", order_by="modified desc")
	if not subject:
		return None, None
	doc = frappe.get_doc("CRM Lead", subject)
	doc.check_permission("read")  # this record's data is what the author is about to read
	# `{}`, never None: nothing is mid-save at author time, so there is no change set — and `context_for`
	# walks `changed.items()` unguarded, which is what makes `None` an AttributeError rather than a default.
	return doc, context_for(doc, changed={})


def _preview_gate(lead, values):
	"""Every preview's front door: may this user author, is there a lead, and what did the wire send.

	`values` arrives as JSON text from the browser and as a list from a test — parsed once, here, so
	neither preview has to know which caller it has.
	"""
	if not frappe.has_permission("CRM Workflow", "write"):
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)
	rows = frappe.parse_json(values) if isinstance(values, str) else values
	doc, ctx = _preview_context(lead)
	if not doc:
		return None, None, None, {"error": frappe._("There is no lead to build a preview from yet.")}
	return doc, ctx, rows or [], None


@frappe.whitelist()
def whatsapp_template_preview(template, values=None, lead=None):
	"""What this node would really send, for one real lead. The author's answer to "is it filled in?".

	THE SAME PATH A journey TAKES — `_template_parameters`, unchanged, against the account this lead's grain
	really routes to. A preview that filled the slots its own way would be confidently wrong, which is
	worse than no preview; and `blank` here is the SAME verdict that routes the live send to `failed`, so
	an author sees the hole before a patient does rather than after.

	The BODY is returned as the provider stores it, never substituted here: WhatsApp assembles the final
	message provider-side from the template plus the parameters, so rendering it locally would be our
	reproduction of someone else's renderer. The control shows the body and the values it would carry.
	"""
	doc, ctx, rows, refusal = _preview_gate(lead, values)
	if refusal:
		return refusal

	from tatva_connect.channels import resolve
	from tatva_connect.whatsapp import routing

	account_name = routing.resolve_account_for_lead(doc)
	if not account_name:
		return {"lead": doc.name, "error": frappe._("No WhatsApp account is routed for this lead's grain.")}
	account = frappe.get_doc("WhatsApp Account", account_name)
	row = frappe.get_doc("WhatsApp Templates", template)
	mismatch = template_account_mismatch(template, account_name)
	if mismatch:
		return {"lead": doc.name, "error": mismatch}

	body = {"lead": doc.name, "body": row.get("template") or ""}
	# A slot with no row RAISES in the filler, and mid-mapping that is the author's ordinary state — so it
	# is REPORTED here rather than 500ing the panel they are still filling in.
	try:
		parameters, blank = _template_parameters(resolve.adapter_for(account), account, row, rows, ctx)
	except ValueError as unmapped:
		return {**body, "error": str(unmapped)}
	return {**body, "values": parameters, "blank": blank}


@frappe.whitelist()
def email_template_preview(template, values=None, lead=None):
	"""The subject and body this node would really send, rendered. The EMAIL TWIN, not a branch.

	Exact where WhatsApp cannot be: `frappe.render_template` over the `Email Template`'s own subject and
	body is literally what `send_email` calls, so this is the message, not a reproduction of it. Filled
	through `_slot_values` — the same function, so the preview and the send cannot disagree about a slot.
	"""
	doc, ctx, rows, refusal = _preview_gate(lead, values)
	if refusal:
		return refusal

	row = frappe.db.get_value("Email Template", template, ["subject", "use_html", "response_html", "response"], as_dict=True)
	if not row:
		return {"lead": doc.name, "error": frappe._("That template no longer exists.")}
	# Same reason as WhatsApp: an unmapped slot is author state, not a server error.
	try:
		filled, blank = _slot_values(template, email_template_slots(template), rows, ctx)
	except ValueError as unmapped:
		return {"lead": doc.name, "error": str(unmapped)}
	body = row.response_html if row.use_html else row.response
	# Rendered only for what resolved: a blank slot is REPORTED, never quietly rendered as an empty string.
	return {
		"lead": doc.name,
		"subject": frappe.render_template(row.subject or "", filled) if not blank else (row.subject or ""),
		"body": frappe.render_template(body or "", filled) if not blank else (body or ""),
		"blank": blank,
	}


def email_template_slots(template):
	"""The named slots an `Email Template` really has — the EMAIL TWIN of `template_slots`, not a branch.

	A WhatsApp template carries positional `{{1}}` slots the provider declares; an Email Template carries
	NAMED Jinja variables in its subject and its body. Same mapping control for the author, two readers,
	because the two questions have two different sources of truth.

	Read with Jinja's own parser rather than a regex: `meta.find_undeclared_variables` is what actually
	decides what a template will ask for at render time, so the author is offered exactly the names
	`frappe.render_template` will look up. A regex would drift from the renderer the first time a template
	used a filter or a block.
	"""
	if not frappe.has_permission("CRM Workflow", "read"):
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)
	row = frappe.db.get_value("Email Template", template, ["subject", "use_html", "response_html", "response"], as_dict=True)
	if not row:
		return []

	from jinja2 import Environment, meta

	env = Environment(autoescape=True)  # parsed for variable names only and never rendered; autoescape is set anyway so no scanner has to be told a second time
	body = row.response_html if row.use_html else row.response
	found = set()
	for source in (row.subject, body):
		found |= meta.find_undeclared_variables(env.parse(source or ""))
	return sorted(found)


def send_email(subject_lead, contact_email, template_name, context=None, values=None):
	"""Queue (or, while dormant, record) a templated email. Returns `(output, marker)`.

	The structural twin of `send_whatsapp`: the recipient is a DECLARED reference, the body comes from a
	picked `Email Template`, and the template's named slots are filled from the node's declared mapping.

	`contact_email` is resolved as a PURE reference. The old `resolve_recipient` is deleted rather than
	reused: it treated anything that did not look namespaced as a literal address, so a typed string was
	mailed as-is. That is the same hole as the typed phone number that reached the wrong subscriber.

	No `now=True`: `frappe.sendmail` only inserts an Email Queue row, a normal DB write that rides the
	rule's own segment transaction, so a rolled-back segment sends nothing.
	"""
	if not template_name:
		raise ValueError("Send Email action has no Email Template configured")
	address = (context or {}).get(contact_email) if contact_email else None
	if not address:
		return FAILED, f"failed: {contact_email or 'no recipient'} resolved to no address for lead {subject_lead}"

	slots = email_template_slots(template_name)
	filled, blank = _slot_values(template_name, slots, values, context if context is not None else {})
	if blank:
		return FAILED, "failed: {} resolved to nothing, so the email would have gone out with a blank in it".format(
			", ".join(sorted(blank))
		)

	if not sends_enabled():
		return SENT, DORMANT_MARKER

	row = frappe.db.get_value("Email Template", template_name, ["subject", "use_html", "response_html", "response"], as_dict=True)
	body = row.response_html if row.use_html else row.response
	frappe.sendmail(
		recipients=[address],
		subject=frappe.render_template(row.subject or "", filled),
		message=frappe.render_template(body or "", filled),
		reference_doctype="CRM Lead",
		reference_name=subject_lead,
	)
	return SENT, f"queued: to={address}"


def _slot_values(template_name, slots, values, ctx):
	"""Fill the template's named slots from the node's DECLARED mapping. Returns `(filled, blank)`.

	The same split `_template_parameters` makes for WhatsApp, for the same reasons: a slot with NO declared
	row RAISES because it is author error wrong for every record equally, while a declared row that
	RESOLVES BLANK routes to `failed` because that is a data state - and either way nothing goes out with
	a hole in it.
	"""
	from tatva_connect.workflow_engine import contract

	declared = contract.value_rows_map(values)
	missing = missing_value_rows(slots, values)
	if missing:
		raise ValueError(
			"Send Email: template {} has no value declared for {} - every slot needs a row".format(
				template_name, ", ".join(missing)
			)
		)

	filled, blank = {}, []
	for name in slots:
		mode, value = declared[name]
		resolved = ctx.get(value) if mode == contract.FROM_CONTEXT else value
		if resolved is None or str(resolved) == "":
			blank.append(name)
			continue
		filled[name] = str(resolved)
	return filled, blank


def _agent_variables(account, agent_id, values, ctx):
	"""Fill the agent's placeholders from the author's DECLARED rows. Returns `(variables, blank)`.

	The exact contract `_template_parameters` holds for WhatsApp, for the exact same reason. A slot with NO
	declared row RAISES — author error, wrong for every lead equally. A row that resolves BLANK is returned
	in `blank` and routes to `failed`, because that is a DATA state (this patient has no program recorded)
	and the one thing this exists to prevent is a placeholder reaching a patient. Live proof it matters:
	Bolna substitutes `{customer_name}` literally, so an unfilled slot is the agent saying the words
	"Hi customer name" down the phone.

	A provider we cannot reach returns no slot names, so nothing is refused on our inability to ask.
	"""
	from tatva_connect.voice import api as voice_api
	from tatva_connect.workflow_engine import contract

	try:
		names = voice_api.agent_variables_for(account, agent_id)
	except Exception:
		frappe.log_error(title="voice: could not read agent placeholders", message=frappe.get_traceback())
		return {}, []
	if not names:
		return {}, []

	missing = missing_value_rows(names, values)
	if missing:
		raise ValueError(
			"AI Voice Call: agent {} has no value declared for {} - every placeholder needs a row".format(
				agent_id, ", ".join(missing)
			)
		)

	declared = contract.value_rows_map(values)
	variables, blank = {}, []
	for name in names:
		mode, value = declared[name]
		resolved = ctx.get(value) if mode == contract.FROM_CONTEXT else value
		if resolved is None or str(resolved) == "":
			blank.append(name)
			continue
		variables[name] = str(resolved)
	return variables, blank


def send_voice(subject_lead, contact_number, connection, agent_id, context=None, from_override=None,
               values=None, correlation=None):
	"""Place an outbound AI voice call to `subject_lead`, or route on why it did not happen. The structural
	twin of `send_whatsapp`: the recipient is a DECLARED reference conformed by the channel's declared
	format, the send is behind the SAME dormant `Workflow::Engine::sends` gate, and the provider call is
	DEFERRED past commit via a thunk (`_deliver_voice`) so a rolled-back segment dials nothing.

	`correlation` is the engine token for THIS node — carried to the provider in `user_data` so the terminal
	webhook wakes this journey and no other.

	GUARDS, IN ORDER, and the order is the point: no number → failed; no country code → REFUSED, never
	dialled, before any switch is read; sends gate OFF → suppressed `placed`, no adapter touched; VOICE
	CHANNEL OFF → failed, no call (the same second gate `send_whatsapp` has always had); account not
	enabled → failed, no call. Only past all five is anything handed to the provider.
	"""
	number = (context or {}).get(contact_number) if contact_number else None
	if not number:
		return FAILED, f"failed: {contact_number or 'no recipient'} resolved to no number for lead {subject_lead}"
	_record_contact(context, VOICE, number)

	# Wrong-country prevention, voice form. `conform_number` is the ONE brain (the provider's declared
	# `number_format`); a number with no country code cannot be known correct and is refused before any gate.
	from tatva_connect.voice.adapters import bolna

	dialable = bolna.DECLARATION.conform_number(number)
	if not dialable:
		return FAILED, f"failed: {number} has no country code, so it cannot be dialled safely"

	if not sends_enabled():
		return PLACED, DORMANT_MARKER

	# The CHANNEL's own master switch, on top of the sends gate — exactly what `send_whatsapp` does above.
	# Without it the switch governed the inbound webhook only, so an operator who turned voice off stopped
	# hearing about calls and went on placing them. `failed`, not a dormant marker: the sends gate is armed,
	# so this is a deliberate operator decision about THIS channel and the author's `failed` edge is the
	# honest place for it.
	from tatva_connect.voice import channel

	if not channel.is_enabled():
		return FAILED, "failed: the voice channel is switched off"

	if not frappe.db.get_value("CRM AI Voice Account", connection, "enabled"):
		return FAILED, f"failed: voice account {connection or '(none)'} is not enabled"

	# The agent's placeholders, resolved BEFORE the call is deferred: a slot this lead cannot fill is a
	# routing answer, not a background-job failure, so it takes `failed` here rather than dialling and
	# speaking the placeholder aloud.
	variables, blank = _agent_variables(connection, agent_id, values, context or {})
	if blank:
		return FAILED, f"failed: {', '.join(blank)} resolved blank for lead {subject_lead}, so the agent would speak a gap"

	# Deferred exactly like `_deliver_whatsapp`: the provider call fires only after the segment commits, on
	# the `workflow` lane, so a rolled-back segment enqueues nothing and dials nothing.
	return PLACED, lambda: frappe.enqueue(
		"tatva_connect.automation.sends._deliver_voice",
		queue="workflow",
		enqueue_after_commit=True,
		account_name=connection,
		to_number=dialable,
		agent_id=agent_id,
		from_override=from_override,
		lead=subject_lead,
		correlation=correlation,
		variables=variables,
	)


def _deliver_voice(account_name, to_number, agent_id, from_override, lead, correlation=None, variables=None):
	"""The deferred single call `send_voice` enqueues (R1). Runs in the background job after the segment
	commits — a rolled-back segment never reaches it, so no dial. Places ONE call and records the
	execution_id for AUDIT: the wake correlation rides the `user_data` echo, so there is no lookup row to
	write and no commit-race — the terminal webhook reads the engine token straight back out.

	A RAISE HERE IS THE CORRECT BEHAVIOUR, not a bug to smooth over. The journey already took the `placed`
	edge and cannot be walked back; the failure belongs on the RQ failed registry where it is visible and
	replayable, exactly as `_deliver_whatsapp` argues. What frees a journey parked behind a call that never
	happened is the Wait's own timeout leg, or the catch-up reconciler — never a rewritten output here.
	"""
	from tatva_connect.voice import api as voice_api
	from tatva_connect.voice.adapters import bolna

	result = bolna.place_call(
		voice_api.connection_for(account_name), to_number, agent_id, from_override, correlation,
		variables=variables,
	)
	_log_voice_placement(correlation, account_name, result.get("correlation_id"), lead)
	# The call goes onto the LEAD, in the table every other call lands in, in THIS job — one background
	# job places and logs, so the row and the call can never disagree about whether it happened.
	_write_voice_call_log(result, account_name, lead)


def _write_voice_call_log(result, account_name, lead):
	"""WRITE ONE: the call's row on the lead, the moment the provider accepts it.

	Through `bridge._new_call_log`, the ONE writer of an outbound call row — a rep's click-to-call and
	automation's call are the same kind of thing and there is not a second builder for the second kind.
	The execution_id becomes the row's `id`, which is UNIQUE and is what the doctype autonames from, so
	the terminal callback finds it by primary key.

	Best-effort: a call really was placed, and losing its row must not fail the job and re-dial a patient.
	"""
	from tatva_connect.telephony import bridge
	from tatva_connect.voice.adapters import bolna

	execution_id = result.get("correlation_id")
	if not execution_id:
		return
	try:
		bridge._new_call_log(
			to_number=result.get("contact"),
			# Already resolved by `place_call`; passed back rather than re-derived, so nothing on this
			# path re-reads the account to find out which number it dialled from.
			agent_number=result.get("from_phone"),
			account_name=account_name,
			ref_doctype="CRM Lead",
			ref_name=lead,
			medium=bolna.CALL_MEDIUM,
			call_id=execution_id,
			# Automation has no session user, and recording a background job's identity as the person who
			# called the patient would be a lie on the lead's timeline.
			caller=None,
			# `custom_telephony_account` links a CRM Telephony Account; an AI call has none.
			account_field=None,
		)
	except Exception:
		frappe.log_error(title="voice: call log write failed",
		                 message=f"lead={lead} execution_id={execution_id}\n{frappe.get_traceback()}")


def _log_voice_placement(correlation, account_name, execution_id, lead):
	"""One audit row on the journey's own step log: which provider execution this node's call became.

	It is also what the catch-up reconciler reads back — the journey is parked on the engine token, and this is
	the only place the provider's id for that token is durably recorded. Best-effort: a call that really
	was placed must not be reported as failed because its audit row could not be written.
	"""
	journey, _, node_id = (correlation or "").partition("::")
	if not (journey and node_id and frappe.db.exists("CRM Workflow Journey", journey)):
		frappe.logger("workflow_voice").info(
			f"voice call placed outside a journey: lead={lead} execution_id={execution_id}"
		)
		return
	try:
		frappe.get_doc({
			"doctype": "CRM Workflow Step Log",
			"journey": journey,
			"subject_name": lead,
			"node_id": node_id,
			"node_type": "AI Voice Call",
			"outcome": PLACED,
			"detail": f"execution_id={execution_id} account={account_name}",
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — workflow engine, scheduler/queue context
	except Exception:
		frappe.log_error(title="voice: placement audit row failed", message=frappe.get_traceback())
