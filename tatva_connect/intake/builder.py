"""Intake scaffolder — turns a `CRM Intake Form` contract into its runtime sink.

Each intake form gets its OWN `custom=1` DocType (the submission table) plus a
public Web Form bound to it (the two fronts, one sink — see the architecture plan).
Both are created live, from data, with no deploy: the per-form DocType name is
DERIVED from the form name, never stored, so there is no schema change to ship.

`sync_form(cfg)` is idempotent: it creates the DocType + Web Form on first sync and
adds any missing fields on re-sync. It NEVER drops a column (data safety) and NEVER
builds DDL from user strings — every name is validated against frappe's own DocType
name rules first, and all writes go through the DocType / Web Form document API.
"""
import re

import frappe
from frappe import _

# Fields every per-form submission table carries, independent of the contract.
# `intake_form` is the back-link the wildcard router reads to resolve the contract;
# `phone` is the lead dedup anchor the fold always reads (intrinsic to every intake);
# `prescription` is the optional attachment the fold surfaces onto the lead.
_INTAKE_FORM_FIELD = "intake_form"
_PHONE_FIELD = "phone"
_PRESCRIPTION_FIELD = "prescription"

# Mirror of frappe's own DocType-name rule (doctype.py START_WITH_LETTERS_PATTERN):
# start with a letter, then letters / digits / space / underscore / hyphen. We
# validate against this BEFORE any insert so a hostile form name can never reach DDL.
_DOCTYPE_NAME_RE = re.compile(r"^(?![\W])[^\d_\s][\w -]+$", flags=re.ASCII)
_MAX_DOCTYPE_NAME = 61  # `tab<name>` must fit MySQL's 64-char table-name limit


def doctype_name_for(cfg) -> str:
	"""The per-form DocType name, derived from the form name and validated.

	Derived (never stored) so there is no doctype-JSON change to ship; resolving the
	contract from a submission row goes the other way, via the row's `intake_form` link.
	"""
	name = safe_doctype_name_for(cfg)
	if not name:
		frappe.throw(
			_("Form Name '{0}' does not yield a valid DocType name.").format(cfg.form_name),
			title=_("Invalid Form Name"),
		)
	return name


def safe_doctype_name_for(cfg) -> str | None:
	"""doctype_name_for without raising — returns None for a name that can't be a valid
	DocType (e.g. a legacy free-text form name). Used by the wildcard router's guard-set
	builder, which must never crash a site-wide insert on one bad row."""
	name = "Intake " + (cfg.form_name or "").strip()
	if len(name) > _MAX_DOCTYPE_NAME or not _DOCTYPE_NAME_RE.match(name):
		return None
	return name


def _safe_fieldname(fieldname: str) -> str:
	"""A submission column name must be a plain identifier — letters, digits, _ ;
	starting with a letter. We never build a column from a raw form string, only from
	a name that has passed this gate (defence in depth even though sources are config)."""
	if not fieldname or not re.match(r"^[a-z][a-z0-9_]*$", fieldname, flags=re.ASCII):
		frappe.throw(_("Unsafe source field name: '{0}'").format(fieldname), title=_("Invalid Field"))
	return fieldname


def _source_fields(cfg) -> list[str]:
	"""The submission-side columns the contract declares — the distinct `source_field`
	(and `manual_field`) of every mapping row. Validated as safe identifiers."""
	names: list[str] = []
	for m in cfg.mappings:
		for fn in (m.source_field, m.manual_field):
			if fn and fn not in names:
				names.append(_safe_fieldname(fn))
	return names


def _docfields(cfg) -> list[dict]:
	"""The full DocField list for the per-form submission DocType: the hidden back-link,
	the prescription attachment, then one Data column per declared source field."""
	fields = [
		{
			"fieldname": _INTAKE_FORM_FIELD,
			"label": "Intake Form",
			"fieldtype": "Data",
			"hidden": 1,
			"read_only": 1,
			"default": cfg.name,
		},
		{"fieldname": _PHONE_FIELD, "label": "Patient Phone Number", "fieldtype": "Phone", "reqd": 1},
		{"fieldname": _PRESCRIPTION_FIELD, "label": "Upload Prescription", "fieldtype": "Attach"},
	]
	for fn in _source_fields(cfg):
		fields.append({"fieldname": fn, "label": frappe.unscrub(fn), "fieldtype": "Data"})
	return fields


def _ensure_doctype(cfg) -> str:
	"""Create the per-form `custom=1` DocType on first sync; on re-sync add only the
	missing fields (never drop). Returns the doctype name."""
	dt = doctype_name_for(cfg)
	wanted = _docfields(cfg)

	if not frappe.db.exists("DocType", dt):
		doc = frappe.get_doc(
			{
				"doctype": "DocType",
				"name": dt,
				"module": "Intake",
				"custom": 1,
				"naming_rule": "Autoincrement",
				"autoname": "autoincrement",
				"fields": wanted,
				"permissions": [
					{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}
				],
			}
		)
		doc.insert(ignore_permissions=True)
		return dt

	# Re-sync: append any field the contract added since the last sync. Additive only.
	meta = frappe.get_meta(dt)
	missing = [f for f in wanted if not meta.has_field(f["fieldname"])]
	if missing:
		doc = frappe.get_doc("DocType", dt)
		for f in missing:
			doc.append("fields", f)
		doc.save(ignore_permissions=True)
	return dt


def _depends_on(field: str, op: str, value: str) -> str | None:
	"""Compile a STRUCTURED show-if (field/op/value) into a Frappe `eval:` JS expression for
	a Web Form Field's depends_on. Never built from free text — only from the validated
	op + an identifier field name + a JSON-escaped value — so there is no eval injection."""
	field = _safe_fieldname((field or "").strip()) if (field or "").strip() else ""
	if not field or not op:
		return None
	if op == "is_checked":
		return "eval:doc.{0}".format(field)
	if op == "is_not_checked":
		return "eval:!doc.{0}".format(field)
	if op == "is_empty":
		return "eval:!doc.{0}".format(field)
	if op == "is_not_empty":
		return "eval:doc.{0}".format(field)
	if op == "equals":
		# frappe.as_json gives a safely-quoted JS string literal (escapes quotes/backslashes)
		# — the comparison value never reaches the expression as raw text.
		return "eval:doc.{0}=={1}".format(field, frappe.as_json(value or ""))
	return None


def _show_if_by_source(cfg) -> dict:
	"""{source_field: mapping} so a Web Form Field input can pick up its own show-if. The
	source_field is the Web Form Field's fieldname; show_if_field references another such field."""
	out = {}
	for m in cfg.mappings:
		if m.source_field and m.get("show_if_op"):
			out.setdefault(m.source_field, m)
	return out


def _web_form_fields(cfg, source_fields: list[str]) -> list[dict]:
	"""Web Form Field rows — one input per declared source field, plus the prescription
	attach. The hidden back-link is NOT a form input (it's defaulted server-side). Each
	field's structured show-if is compiled to depends_on (+ mandatory_depends_on if reqd)."""
	show_if = _show_if_by_source(cfg)
	rows = [{"fieldname": _PHONE_FIELD, "label": "Patient Phone Number", "fieldtype": "Phone", "reqd": 1}]
	for fn in source_fields:
		row = {"fieldname": fn, "label": frappe.unscrub(fn), "fieldtype": "Data"}
		m = show_if.get(fn)
		if m:
			dep = _depends_on(m.get("show_if_field"), m.get("show_if_op"), m.get("show_if_value"))
			if dep:
				row["depends_on"] = dep
				if m.get("reqd"):
					row["mandatory_depends_on"] = dep
		rows.append(row)
	rows.append({"fieldname": _PRESCRIPTION_FIELD, "label": "Upload Prescription", "fieldtype": "Attach"})
	return rows


# Contract field -> real `tabWeb Form` column. The CRM Intake Form's tabbed editor is just a
# clean front over these columns (one sink, no shadow representation): copy each across verbatim.
# `introduction` -> `introduction_text` is the one rename (Text Editor reused on both sides).
# Access flags (anonymous / login_required / allow_multiple) come from the contract too, with a
# code fallback below so a blank/legacy contract still yields a sane anonymous, no-login form.
_SETTINGS_COLUMNS = (
	"anonymous",
	"login_required",
	"allow_multiple",
	"allow_edit",
	"allow_comments",
	"allow_print",
	"show_attachments",
	"max_attachment_size",
	"allowed_embedding_domains",
	"hide_navbar",
	"hide_footer",
	"button_label",
	"show_sidebar",
	"meta_title",
	"meta_description",
	"success_url",
	"banner_image",
	"success_title",
	"success_message",
)


def _web_form_route(cfg) -> str:
	"""Operator route wins (Route field on the Form tab); else derive from the form name.
	The derived value is written back so the field shows the live route after first sync."""
	route = (cfg.get("route") or "").strip()
	return route or frappe.scrub(cfg.form_name).replace("_", "-")


def _ensure_web_form(cfg, dt: str) -> str:
	"""Create / update the public Web Form bound to the per-form DocType. Idempotent:
	rebuilt from the contract each sync — the contract is the ONE writer of these columns.
	Access defaults (anonymous / no-login / multiple) live as a code fallback so a blank
	contract is still sane; an operator value always wins. Returns the Web Form name."""
	source_fields = _source_fields(cfg)

	values = {
		"title": cfg.form_name,
		"route": _web_form_route(cfg),
		"doc_type": dt,
		# Code fallback (Invariant A.4): structural defaults when the contract leaves them blank.
		"anonymous": 1 if cfg.get("anonymous") is None else cfg.get("anonymous"),
		"login_required": cfg.get("login_required") or 0,
		"allow_multiple": 1 if cfg.get("allow_multiple") is None else cfg.get("allow_multiple"),
		# Banner / after-submit (Text Editor on both sides; introduction -> introduction_text).
		"introduction_text": cfg.get("introduction"),
		"web_form_fields": _web_form_fields(cfg, source_fields),
		# List columns reference real DocType fields; the back-link is a safe, present column.
		"list_columns": [{"fieldname": _INTAKE_FORM_FIELD, "label": "Intake Form", "fieldtype": "Data"}],
	}
	# The remaining Settings columns map 1:1 to real tabWeb Form columns — copy verbatim.
	# (anonymous / login_required / allow_multiple handled above with their code fallback.)
	for col in _SETTINGS_COLUMNS:
		if col in ("anonymous", "login_required", "allow_multiple"):
			continue
		values[col] = cfg.get(col)

	existing = frappe.db.get_value("Web Form", {"doc_type": dt}, "name")
	if existing:
		wf = frappe.get_doc("Web Form", existing)
		wf.update(values)
		wf.save(ignore_permissions=True)
		return wf.name

	wf = frappe.get_doc(dict(doctype="Web Form", **values))
	wf.insert(ignore_permissions=True)
	return wf.name


def sync_form(cfg, method=None):
	"""Scaffold (or re-sync) the runtime sink for one `CRM Intake Form`.

	Server-internal only — callers are the form's own controller / an operator action,
	both already System-Manager gated (the builder doctype is System-Manager-only).
	Returns (doctype_name, web_form_name)."""
	dt = _ensure_doctype(cfg)
	wf = _ensure_web_form(cfg, dt)

	# Stamp the derived sink back onto the contract (read-only fields on the Form tab) so the
	# editor shows the live submission DocType and the resolved route. db_set avoids re-running
	# validate/save — the scaffolder stays the one writer of the Web Form, not a save loop.
	if cfg.get("web_form_doctype") != dt:
		cfg.db_set("web_form_doctype", dt, update_modified=False)
	resolved_route = _web_form_route(cfg)
	if cfg.get("route") != resolved_route:
		cfg.db_set("route", resolved_route, update_modified=False)

	from tatva_connect.intake.intake import bust_intake_doctype_cache

	bust_intake_doctype_cache()
	return dt, wf
