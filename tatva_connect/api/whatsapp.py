"""Whitelisted endpoints for the WATI template-variable fill dialog.

Both read/write only local data — no WATI template fetch at send time, so no
rate-limit exposure. Called from the CRM Form Script (a fixture), not from any
crm source change.
"""
import json
import re

import frappe
from frappe import _

from tatva_connect.whatsapp import roles


@frappe.whitelist()
def whatsapp_access():
	"""Does the current user hold a WhatsApp capability role? Drives the SPA tab gate — the
	per-USER half (lead_has_route is the per-lead half). Capability only, never lead access:
	the same allow-list the server enforces on every send (whatsapp_capability_roles)."""
	return {"has_role": bool(set(roles.CAPABILITY_ROLES) & set(frappe.get_roles()))}


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
	rows = frappe.get_list(
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
	from crm.api.whatsapp import validate_access

	validate_access()  # WhatsApp capability gate — template internals are not for non-WhatsApp users
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
		parsed = json.loads(sample_values)  # ALLOWLIST 2026-06-29: keep raw — parse_json won't raise, would dead-path the legacy CSV fallback; do NOT convert.
	except Exception:
		return [str(i + 1) for i in range(len(sample_values.split(",")))]
	if isinstance(parsed, dict):
		return [str(k) for k in parsed.keys()]
	if isinstance(parsed, list):
		return [str(i + 1) for i in range(len(parsed))]
	return []


def _recipient_number(reference_doctype, reference_name):
	"""The record's own number — the ONE permitted WhatsApp recipient. A template must never be
	sent to a client-supplied arbitrary number through the org's WATI sender."""
	if reference_doctype == "CRM Lead":
		return frappe.db.get_value("CRM Lead", reference_name, "mobile_no")
	return None


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
		mobile_no = _recipient_number(reference_doctype, reference_name)
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


@frappe.whitelist()
def failed_reasons(reference_doctype, reference_name):
	"""{message_name: failure_reason} for this record's failed WhatsApp messages.
	Feeds the chat-tab hover tooltip (the message id is the bubble's DOM id)."""
	from crm.api.whatsapp import validate_access

	validate_access(reference_doctype, reference_name)
	rows = frappe.get_list(
		"WhatsApp Message",
		filters={"reference_doctype": reference_doctype, "reference_name": reference_name, "status": "failed"},
		fields=["name", "custom_failed_reason"],
	)
	return {r.name: r.custom_failed_reason for r in rows if r.custom_failed_reason}


def _parse_hints(sample_values):
	"""sample_values is a JSON object keyed by param name ({"1": "...", ...}).

	Falls back to legacy comma-joined values (positional) for rows synced before
	the JSON format. Returns a {str(index): hint} map.
	"""
	if not sample_values:
		return {}
	try:
		parsed = json.loads(sample_values)  # ALLOWLIST 2026-06-29: keep raw — parse_json won't raise, would dead-path the legacy CSV fallback; do NOT convert.
	except Exception:
		parts = [h.strip() for h in sample_values.split(",")]
		return {str(i + 1): v for i, v in enumerate(parts)}
	if isinstance(parsed, dict):
		return {str(k): v for k, v in parsed.items()}
	if isinstance(parsed, list):
		return {str(i + 1): v for i, v in enumerate(parsed)}
	return {}


@frappe.whitelist()
def get_field_options(reference_doctype, reference_name):
	"""Lead + profile field values for the variable-mapping dropdown, grouped by the lead brain's sections.

	The field SET is the ONE lead brain — the same server projection the Data tab renders
	(`lead.detail._select`): only fields the caller is entitled to see FOR THIS LEAD'S GRAIN. It is
	NOT a raw doctype-meta walk, which would leak an uncatalogued or foreign-grain field (PII) straight
	into a template variable and a real WhatsApp send. Each option carries the field's CURRENT value so
	picking it fills the variable; a multi-row child section contributes its LATEST row (the brain's
	single-row rule). Read-only.
	"""
	from crm.api.whatsapp import validate_access

	from tatva_connect.lead import detail

	# Gate access first (WhatsApp role + lead READ); the brain projection below adds the field-level catalog/grain gate, so a redacted field never reaches the picker.
	validate_access(reference_doctype, reference_name)
	doc = frappe.get_doc(reference_doctype, reference_name)

	groups = {}
	for _fk, row in detail._select(doc).items():
		section = detail._section_of(row)
		value = detail._value(doc, section, row)
		if value in (None, ""):
			continue
		g = groups.setdefault(section.name, {"group": section.title, "order": section.display_order or 0, "options": []})
		df = detail._docfield(section.target_doctype, row.get("fieldname"))
		label = row.get("label") or (df.label if df else None) or row.get("fieldname")
		g["options"].append({"label": label, "value": str(value)})
	return [{"group": g["group"], "options": g["options"]}
	        for g in sorted(groups.values(), key=lambda x: x["order"]) if g["options"]]


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
		# Read the raw Singles row, NOT get_single_value: a missing Int single casts to 0 there, which we'd wrongly read as "disabled". The raw value is None only when the field was never saved -> apply the default cap. An EXPLICIT "0" the operator saved means they disabled this window.
		# ALLOWLIST 2026-06-29: keep raw — get_single_value casts an unset Int to 0 (=disabled); do NOT convert in any sweep.
		raw = frappe.db.get_value(
			"Singles",
			{"doctype": "CRM WhatsApp Settings", "field": field},
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

	# Bind the recipient to the record — never send to a client-supplied arbitrary number.
	from tatva_connect.whatsapp.channel import normalize_number

	expected = _recipient_number(reference_doctype, reference_name)
	if not expected or normalize_number(to) != normalize_number(expected):
		frappe.throw(_("Recipient must match the record's number."), frappe.PermissionError)

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
	doc.insert(ignore_permissions=True)  # authz-ok: tier-b — gated by frappe.has_permission on the lead before the write
	return doc.name


# --- Reconcile a lead's WhatsApp thread against the provider (the "Refresh" button) ---


@frappe.whitelist()
def refresh_messages_from_wati(reference_doctype, reference_name):
	"""Reconcile a lead's WhatsApp thread against the provider's authoritative history.

	This is a thin door onto `whatsapp.backfill`, which is the ONE reconcile: the adapter normalizes a
	history item, `ingest` persists it, and `ingest.held_by_lead` decides what is already here. There is
	deliberately no parsing in this module — a private copy of that rule read v1 field names after the
	adapter moved to v3, matched nothing, and left a delete-then-rebuild that deleted the lead's entire
	thread and reinserted none of it.

	Additive, never destructive: it inserts what is missing and touches nothing that is already here.
	"""
	from crm.api.whatsapp import validate_access

	from tatva_connect.whatsapp import backfill

	validate_access(reference_doctype, reference_name)
	if reference_doctype != "CRM Lead":
		frappe.throw("WhatsApp refresh is only supported on CRM Lead.")

	summary = backfill.backfill_lead(reference_name, dry_run=False)
	if not summary.get("ok"):
		frappe.throw(summary.get("reason") or "WhatsApp refresh is unavailable for this lead.")

	# The backfill writes through ingest, which does not publish on a historical insert, so the open panel is told once here — the same event crm emits on WhatsApp Message.on_update.
	frappe.publish_realtime(
		"whatsapp_message",
		{"reference_doctype": reference_doctype, "reference_name": reference_name},
	)
	return {"count": summary.get("new", 0), "existing": summary.get("existing", 0)}
