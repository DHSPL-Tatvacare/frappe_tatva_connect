"""Whitelisted endpoints for the WATI template-variable fill dialog.

Both read/write only local data — no WATI template fetch at send time, so no
rate-limit exposure. Called from the CRM Form Script (a fixture), not from any
crm source change.
"""
import json
import re

import frappe
from frappe import _


@frappe.whitelist()
def list_templates(reference_doctype=None, reference_name=None):
	"""Approved templates for the picker — SCOPED to the lead's routed WATI account.

	Each WATI tenant has its own template namespace; a template approved on one
	tenant cannot be sent from another. So we resolve the lead's account first and
	list only that account's templates. No route -> empty list (the lead can't be
	sent to anyway; set_whatsapp_account would raise at send time).

	Returns [{name, label, vars, category}] — `name` is the (account-scoped)
	record id passed to the next calls; `label` is the clean template name shown
	to the agent.
	"""
	account = None
	if reference_doctype and reference_name:
		from crm.api.whatsapp import validate_access

		validate_access(reference_doctype, reference_name)
		if reference_doctype == "CRM Lead":
			from tatva_connect.whatsapp import routing

			account = routing.resolve_account_for_lead(
				frappe.get_cached_doc(reference_doctype, reference_name)
			)
		if not account:
			return []

	filters = {"status": "APPROVED"}
	if account:
		filters["whatsapp_account"] = account
	rows = frappe.get_all(
		"WhatsApp Templates",
		filters=filters,
		fields=["name", "actual_name", "template", "category"],
		order_by="category asc, actual_name asc",
	)
	out = []
	for r in rows:
		n = len({int(m) for m in re.findall(r"\{\{\s*(\d+)\s*\}\}", r.template or "")})
		out.append(
			{
				"name": r.name,
				"label": r.actual_name or r.name,
				"vars": n,
				"category": (r.category or "OTHER"),
			}
		)
	return out


@frappe.whitelist()
def get_template_variables(template):
	"""Return the {{N}} slots for a template, parsed from the LOCAL mirror.

	[{index, hint}] where hint is the WATI sample value for that slot (from
	sample_values). No WATI API call.
	"""
	doc = frappe.get_cached_doc("WhatsApp Templates", template)
	body = doc.template or ""
	indexes = sorted({int(m) for m in re.findall(r"\{\{\s*(\d+)\s*\}\}", body)})
	names = _param_names(doc.sample_values)  # WATI paramNames in body order
	hints = _parse_hints(doc.sample_values)  # keyed by paramName
	variables = []
	for i in indexes:
		name = names[i - 1] if 0 < i <= len(names) else str(i)
		variables.append({"index": i, "name": name, "hint": hints.get(name, "")})
	return {"template": template, "body": body, "variables": variables}


def _param_names(sample_values):
	"""Ordered WATI parameter names ({{1}},{{2}},… → real names), the keys of
	sample_values in body order. Empty when none (static template)."""
	if not sample_values:
		return []
	try:
		parsed = json.loads(sample_values)
	except Exception:
		return [str(i + 1) for i in range(len(sample_values.split(",")))]
	if isinstance(parsed, dict):
		return [str(k) for k in parsed.keys()]
	if isinstance(parsed, list):
		return [str(i + 1) for i in range(len(parsed))]
	return []


@frappe.whitelist()
def get_send_context(reference_doctype, reference_name):
	"""One call for the Send-Template dialog: the resolved WATI account (name +
	channel number), the recipient number, and the account-scoped approved
	templates. Account is None when the lead has no WATI route."""
	from crm.api.whatsapp import validate_access

	validate_access(reference_doctype, reference_name)
	account = None
	mobile_no = None
	if reference_doctype == "CRM Lead":
		from tatva_connect.whatsapp import routing

		lead = frappe.get_cached_doc(reference_doctype, reference_name)
		mobile_no = lead.mobile_no
		name = routing.resolve_account_for_lead(lead)
		if name:
			account = {
				"name": name,
				"number": frappe.db.get_value("WhatsApp Account", name, "custom_wati_channel_number"),
			}
	return {
		"account": account,
		"mobile_no": mobile_no,
		"templates": list_templates(reference_doctype, reference_name),
	}


def _parse_hints(sample_values):
	"""sample_values is a JSON object keyed by param name ({"1": "...", ...}).

	Falls back to legacy comma-joined values (positional) for rows synced before
	the JSON format. Returns a {str(index): hint} map.
	"""
	if not sample_values:
		return {}
	try:
		parsed = json.loads(sample_values)
	except Exception:
		parts = [h.strip() for h in sample_values.split(",")]
		return {str(i + 1): v for i, v in enumerate(parts)}
	if isinstance(parsed, dict):
		return {str(k): v for k, v in parsed.items()}
	if isinstance(parsed, list):
		return {str(i + 1): v for i, v in enumerate(parsed)}
	return {}


_VALUE_TYPES = (
	"Data", "Select", "Small Text", "Text", "Link", "Int", "Float",
	"Currency", "Date", "Datetime", "Phone", "Read Only",
)


def _field_options(doc, group):
	opts = []
	for df in doc.meta.fields:
		if df.fieldtype in _VALUE_TYPES and not df.get("hidden"):
			val = doc.get(df.fieldname)
			if val not in (None, ""):
				opts.append({"label": df.label or df.fieldname, "value": str(val)})
	return {"group": group, "options": opts} if opts else None


@frappe.whitelist()
def get_field_options(reference_doctype, reference_name):
	"""Lead + profile field values for the variable-mapping dropdown.

	Grouped (Lead / Plan / Lab / Care Providers); each option carries the
	field's CURRENT value so picking it fills the variable. Read-only.
	"""
	from crm.api.whatsapp import validate_access

	# Gate access: without this any authenticated user could read any lead's
	# field values (PII) through this endpoint.
	validate_access(reference_doctype, reference_name)
	doc = frappe.get_doc(reference_doctype, reference_name)
	groups = []
	lead = _field_options(doc, "Lead")
	if lead:
		groups.append(lead)
	for fieldname, label in (
		("custom_plan_profile", "Plan Profile"),
		("custom_lab_profile", "Lab Profile"),
		("custom_care_providers_profile", "Care Providers"),
	):
		rows = doc.get(fieldname) or []
		if rows:
			g = _field_options(rows[0], label)
			if g:
				groups.append(g)
	return groups


def _enforce_manual_template_cap(reference_doctype, reference_name):
	"""Block a MANUAL template send that exceeds the per-number rate cap.

	Scoped to THIS endpoint (the manual chat-box/picker path), so automated
	WhatsApp Notification sends are never throttled — that's the abuse vector we
	cap, no manual-vs-automated guessing needed. Counts prior Outgoing Template
	rows on the same lead within each rolling window; an unset/<=0 cap disables
	that window. See project_whatsapp_template_rate_cap.
	"""
	from frappe.utils import add_to_date, cint, now_datetime

	now = now_datetime()
	for field, default, hours in (("template_cap_per_hour", 5, 1), ("template_cap_per_day", 10, 24)):
		# Read the raw Singles row, NOT get_single_value: a missing Int single casts
		# to 0 there, which we'd wrongly read as "disabled". The raw value is None
		# only when the field was never saved -> apply the default cap. An EXPLICIT
		# "0" the operator saved means they disabled this window.
		raw = frappe.db.get_value(
			"Singles",
			{"doctype": "Tatva Automation Settings", "field": field},
			"value",
			order_by=None,  # tabSingles has no `modified` column
		)
		cap = default if raw in (None, "") else cint(raw)
		if cap <= 0:
			continue
		count = frappe.db.count(
			"WhatsApp Message",
			{
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"message_type": "Template",
				"type": "Outgoing",
				"creation": [">=", add_to_date(now, hours=-hours)],
			},
		)
		if count >= cap:
			window = _("hour") if hours == 1 else _("24 hours")
			frappe.throw(
				_(
					"Template limit reached — {0} already sent to this patient in the last {1}. "
					"Please wait before sending another, or send a template later."
				).format(count, window),
				title=_("WhatsApp template limit"),
			)


@frappe.whitelist()
def send_template_with_params(reference_doctype, reference_name, template, to, body_param=None):
	"""Send a template with the agent-filled variable values (body_param JSON).

	Creates a WhatsApp Message; our WATIWhatsAppMessage resolver fills {{N}} from
	body_param (a JSON string like {"1": "...", "2": "..."}).
	"""
	from crm.api.whatsapp import validate_access

	validate_access(reference_doctype, reference_name)
	_enforce_manual_template_cap(reference_doctype, reference_name)
	doc = frappe.new_doc("WhatsApp Message")
	doc.update(
		{
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"message_type": "Template",
			"message": "Template message",
			"content_type": "text",
			"use_template": 1,
			"template": template,
			"to": to,
			"body_param": body_param or None,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


# --- Reconcile a lead's WhatsApp thread against WATI (the "Refresh" button) ---

# WATI statusString -> the status vocab the CRM WhatsApp tab renders.
_WATI_STATUS = {
	"SENT": "sent",
	"DELIVERED": "delivered",
	"READ": "read",
	"REPLIED": "read",
	"FAILED": "failed",
}
_MEDIA_TYPES = {"text", "image", "video", "audio", "document"}
_FALSY = (False, "false", "False", 0, "0", None, "")


@frappe.whitelist()
def whatsapp_window_state(reference_doctype, reference_name):
	"""Is the WhatsApp 24-hour customer-service window OPEN for this record?

	OPEN iff the customer sent an inbound WhatsApp message within the last 24h —
	Meta's rule, and the canonical definition (WATI exposes no window flag). Drives
	the UI: when CLOSED, the free-text input box is hidden and only template
	messages may be sent; a new inbound reopens it. Read-only, fail-open-to-closed.
	"""
	from frappe.utils import add_to_date, get_datetime, now_datetime

	from crm.api.whatsapp import validate_access

	validate_access(reference_doctype, reference_name)
	last = frappe.db.get_value(
		"WhatsApp Message",
		{"reference_doctype": reference_doctype, "reference_name": reference_name, "type": "Incoming"},
		"creation",
		order_by="creation desc",
	)
	if not last:
		return {"open": False, "last_inbound": None, "expires_at": None}
	expires = add_to_date(get_datetime(last), hours=24)
	return {
		"open": get_datetime(now_datetime()) < expires,
		"last_inbound": str(last),
		"expires_at": str(expires),
	}


def _row_from_wati_item(it, number, ref_doctype, ref_name):
	"""Translate one WATI getMessages item into a WhatsApp Message row dict.

	`id` is WATI's stable per-message key (present on every item). We keep it as
	`message_id` (status threading keys on it), but the row NAME is scoped per-lead
	(`{ref_name}-{id}`) so one WhatsApp number shared by two leads (same patient in
	two programs) can mirror the same message onto BOTH leads without colliding on
	the primary key or the unique index. A refresh stays idempotent per lead (the
	delete is scoped to this lead, the deterministic name reinserts the same rows).
	Returns None for non-chat items (ticket/assignment events, empty system rows).
	"""
	event_type = it.get("eventType")
	wid = it.get("id")
	if event_type == "ticket" or not wid:
		return None

	status = _WATI_STATUS.get((it.get("statusString") or "").upper(), "")
	created = it.get("created")
	base = {
		"name": f"{ref_name}-{wid}",
		"message_id": wid,
		"creation": created,
		"conversation_id": it.get("conversationId"),
		"reference_doctype": ref_doctype,
		"reference_name": ref_name,
	}

	if event_type == "broadcastMessage":
		# Outbound template (variables already resolved by WATI into finalText).
		base.update(
			{
				"type": "Outgoing",
				"message": it.get("finalText") or "",
				"content_type": "text",
				"status": status or "sent",
				"to": "+" + number,
			}
		)
		return base

	if event_type == "message":
		body = it.get("text") or ""
		ctype = it.get("type") if it.get("type") in _MEDIA_TYPES else "text"
		# Drop pure system/call rows that carry no body and no media.
		if not body and ctype == "text":
			return None
		base.update({"message": body, "content_type": ctype})
		if ctype in _MEDIA_TYPES - {"text"} and it.get("data"):
			base["_media"] = {"wati_id": it.get("id"), "data": it.get("data"), "text": it.get("text"), "type": ctype}
		if it.get("owner") in _FALSY:  # owner falsy = inbound (customer)
			base.update({"type": "Incoming", "from": "+" + number})
		else:
			base.update({"type": "Outgoing", "to": "+" + number, "status": status or "sent"})
		return base

	return None


def _to_system_naive(iso):
	"""WATI timestamps are UTC ISO ('2026-06-02T18:23:35.908Z'). Convert to a
	naive datetime in the site's timezone (what Frappe stores). None on failure."""
	if not iso:
		return None
	try:
		import datetime

		base = str(iso).replace("Z", "").split(".")[0].split("+")[0]
		dt_utc = datetime.datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
		return frappe.utils.convert_utc_to_system_timezone(dt_utc).replace(tzinfo=None)
	except Exception:
		return None


def _insert_history_row(row):
	"""Write a reconciled row directly (db_insert) — NEVER through insert(), which
	would trigger before_insert/send_outgoing and re-send the message via WATI."""
	doc = frappe.new_doc("WhatsApp Message")
	created = _to_system_naive(row.pop("creation", None))
	doc.update(row)
	doc.name = row["name"]
	doc.flags.name_set = True
	if created:
		doc.creation = created
		doc.modified = created
	doc.db_insert()


@frappe.whitelist()
def refresh_messages_from_wati(reference_doctype, reference_name):
	"""Reconcile a lead's WhatsApp thread against WATI's authoritative history.

	Pulls the full conversation (all pages) and rebuilds ONLY this lead's
	WhatsApp Message rows from it — keyed on WATI's stable `id`. Scoped strictly
	to this lead; runs in one transaction (a failed fetch aborts before any
	delete); uses db_insert so nothing is re-sent.
	"""
	from crm.api.whatsapp import validate_access

	from tatva_connect.whatsapp import api as wati
	from tatva_connect.whatsapp import routing

	validate_access(reference_doctype, reference_name)
	if reference_doctype != "CRM Lead":
		frappe.throw("WhatsApp refresh is only supported on CRM Lead.")

	lead = frappe.get_doc(reference_doctype, reference_name)
	account_name = routing.resolve_account_for_lead(lead)
	if not account_name:
		frappe.throw("No WATI account route for this lead — configure routing before refreshing.")
	number = wati.normalize_number(lead.mobile_no)
	if not number:
		frappe.throw("This lead has no mobile number to refresh.")

	account = frappe.get_doc("WhatsApp Account", account_name)
	wati.assert_wati(account)

	# Fetch first — if WATI errors this raises and we never delete anything.
	items = wati.get_all_messages(account, number)
	rows = [
		r for r in (
			_row_from_wati_item(it, number, reference_doctype, reference_name) for it in items
		) if r
	]

	# Transactional rebuild, scoped to THIS lead only.
	frappe.db.delete(
		"WhatsApp Message",
		{"reference_doctype": reference_doctype, "reference_name": reference_name},
	)
	for row in rows:
		media = row.pop("_media", None)
		if media:
			from tatva_connect.whatsapp import media as media_module

			filedoc = media_module.find_lead_media(reference_name, media["wati_id"])
			if not filedoc:
				try:
					content, _ctype = wati.get_media(account, media["data"])
					fname = media_module.media_filename(media["type"], media["text"], media["data"])
					filedoc = media_module.ensure_lead_media(reference_name, media["wati_id"], fname, content)
				except Exception:
					filedoc = None
					frappe.log_error(title="WATI refresh media fetch failed",
					                 message=f"wati_id={media['wati_id']} lead={reference_name}")
			if filedoc:
				row["attach"] = filedoc.file_url
		_insert_history_row(row)
	frappe.db.commit()
	# The reconcile uses direct DB writes (no controller events), so crm's
	# WhatsApp panel never hears about the rebuilt thread. Emit the SAME realtime
	# event crm publishes on WhatsApp Message.on_update -> the open panel re-fetches
	# inline (whatsappMessages.reload()), so the UI needs no page reload.
	frappe.publish_realtime(
		"whatsapp_message",
		{"reference_doctype": reference_doctype, "reference_name": reference_name},
	)
	return {"count": len(rows), "account": account_name}
