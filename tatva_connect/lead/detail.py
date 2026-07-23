# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Lead detail panel projection — the grain/brain-aware, LSQ-clean Data tab.

The Data tab used to render the raw doctype layout (clinical/program child tables as inline
grids) gated by a DOM-hacking CRM Form Script (lead/form_scripts/data_tab_gate.js). This module
replaces that with a clean, server-resolved projection driven by the EXISTING field catalog
(`CRM Lead API Field`) and the SAME grain-entitlement brain Smart Views uses — no new field
meanings, no DOM, no client field enumeration.

Section routing (title, order, target doctype, child table, row key) is NOT restated here: every
catalog row carries a `section` Link and the section's own row (`CRM Lead Section`) is the ONE
home of that routing, read live via frappe.get_cached_doc.

The CATALOG is the single authority. One viewer gate only:
  * ENTITLEMENT (viewer) — may this principal see the field at all?
      access.entitlement.resolve_fields (grain brain + role restriction + universal floor).
Every catalogued profile field for the viewer's grain surfaces, grouped into its section.
Sections are DISPLAY GROUPS only; the frontend's "hide empty fields" toggle keeps the tab neat.

Security:
  * Read is permission-gated; values are resolved server-side (no client SQL, no field
    enumeration) — there is NO string-built SQL in this module; everything flows through the
    frappe doc API.
  * Write goes through a SERVER-BUILT allowlist (entitled ∧ not read-only ∧ not a protected
    routing field). A field_key outside it is rejected — a crafted payload can never reach
    routing/owner/out-of-grain/read-only fields (no mass-assignment).
"""
import re

import frappe
from frappe import _
from frappe.utils import cstr

from tatva_connect.access import entitlement
from tatva_connect.lead import keyvalue, multirow
from tatva_connect.taxonomy import grain, labels

# Identity/routing fields: shown (informative) but NEVER editable on this panel. Identity
# (vertical/group) is the dedup anchor; program transitions happen via deliberate routing
# flows, not a casual field edit. Forced read-only regardless of catalog/property-setter state.
_PROTECTED_FIELDS = frozenset({"custom_vertical", "custom_group", "custom_current_program"})

_CATALOG_FIELDS = [
	"field_key", "label", "fieldname", "section",
]


# ------------------------------- pure helpers (no DB) -------------------------------
def _is_universal(row):
	"""A row is universal iff its field is ticked by every internal contract (belongs to all grains); a
	grain-specific row is not. Specificity is read through the contract brain, never off grain_* directly."""
	return entitlement.is_universal_field(row["field_key"])


def dedup_rows(rows):
	"""Collapse duplicate catalog rows to one per (section, fieldname). A grain-specific row wins
	over a universal one, so the grain-scoped label survives. Order preserved by first appearance."""
	chosen, order = {}, []
	for row in rows:
		key = (row.get("section") or "", row.get("fieldname") or "")
		if key not in chosen:
			chosen[key], _ = row, order.append(key)
			continue
		if _is_universal(chosen[key]) and not _is_universal(row):
			chosen[key] = row
	return [chosen[k] for k in order]


def _is_empty(value):
	if value is None:
		return True
	if isinstance(value, str):
		return value.strip() == ""
	if isinstance(value, (list, tuple, dict)):
		return len(value) == 0
	return False  # 0 / False are real values, never "empty"


def empty_everywhere(values):
	"""The panel's "nothing to show here" flag — NOT a predicate on the ONE displayed value.

	`hideEmpty` is ON by default and the panel drops whatever it is told is empty, taking the field's More
	button with it. A field blank on the latest row but filled on an earlier one therefore had its history
	made unreachable. Empty means empty in EVERY row the field is kept in; decided on the SERVER because
	the flag the panel filters on is served, and a second opinion in the client would be a rival brain.
	One rule, both shapes: each branch hands it the rows it keeps."""
	return all(_is_empty(v) for v in values)


_SECTION_SEPARATORS = re.compile(r"[:#]")


def parse_field_key(field_key):
	"""A field key as (section_key, member) — the ONE parse, for BOTH shapes.

	`acq:utm_campaign` and `screening#<identity>` differ only in the separator their shape happens to use,
	and the separator decides NOTHING: what a key addresses is decided by the SECTION it names. Splitting
	on `#` alone read `acq:utm_campaign` as a section called "acq:utm_campaign" and the lookup blew up."""
	parts = _SECTION_SEPARATORS.split(cstr(field_key), maxsplit=1)
	return parts[0], (parts[1] if len(parts) > 1 else "")


# ------------------------------- frappe-bound core -------------------------------
def _catalog_rows():
	"""Every catalog row, keyed by field_key. Read here (not via smartview) so this module has
	no coupling to the Smart Views composer."""
	rows = frappe.get_all("CRM Lead API Field", fields=_CATALOG_FIELDS, order_by="field_key asc")
	return {r["field_key"]: r for r in rows}


def _section_of(row):
	"""The native CRM Lead Section Document a catalog row routes through — the ONE home of its
	title, order, target doctype, child table and row key. No copy, no parser, read live."""
	return frappe.get_cached_doc("CRM Lead Section", row.get("section"))


def _docfield(target_doctype, fieldname):
	try:
		return frappe.get_meta(target_doctype).get_field(fieldname)
	except Exception:
		return None


def _is_readonly(section, fieldname):
	"""Read-only iff a protected routing field, the section's own row key, the docfield says so, or the
	field is unknown (fail-closed: an unresolvable field is never writable).

	A multi-row section's row key is the row's ADDRESS, not a value on it: editing it re-keys the row, so
	the next write naming the original key appends a duplicate instead of updating. Read off the section
	that declares it, never restated here — a single-row section declares none and loses nothing."""
	if fieldname in _PROTECTED_FIELDS or fieldname == (section.row_key_field or ""):
		return True
	df = _docfield(section.target_doctype, fieldname)
	return bool(df.read_only) if df else True


def writable_keys(selected, is_readonly):
	"""The field_keys writable through update_lead_detail: those whose target field is not read-only.
	Target doctype is read off the section brain; `is_readonly(target_doctype, fieldname)` is injected."""
	out = set()
	for fk, row in selected.items():
		if not is_readonly(_section_of(row), row.get("fieldname") or ""):
			out.add(fk)
	return out


def _select(doc):
	"""The entitled (VIEWER's grains) ∧ applicable (THIS LEAD's grain) ∧ deduped {field_key: row}.
	Two grain axes, ONE brain (entitlement.field_in_grains_via_contract): a field shows only if the VIEWER may see
	it AND it belongs to the LEAD's grain — so an Anaya lead never shows Tatvapractice fields even for
	an admin entitled to every grain. Universal keys always pass. (Sections are display groups; the
	frontend hides empties.)"""
	visible = entitlement.resolve_fields(_catalog_rows(), entitlement.entitled_grains(), frappe.get_roles())
	lead_grain = (doc.get("custom_vertical") or "", doc.get("custom_group") or "",
	              doc.get("custom_current_program") or "")
	applicable = {k: r for k, r in visible.items()
	              if _is_universal(r) or entitlement.field_in_grains_via_contract(r["field_key"], [lead_grain])}
	deduped = dedup_rows(list(applicable.values()))
	return {r["field_key"]: r for r in deduped}


def _child_row(doc, section):
	"""The single child row a child-section field reads from. A multi-row section picks the latest via
	the ONE shared rule (multirow.latest_child_row); a single-row section takes the one row. Returns a
	child doc or None."""
	table = section.child_table_field
	if not table:
		return None
	children = doc.get(table) or []
	if not children:
		return None
	if section.is_multi_row and section.row_key_field:
		return multirow.latest_child_row(children, section.row_key_field)
	return children[0]


def _has_history(doc, section):
	"""Does a field of this section have anything behind the value the panel shows?

	Only a multi-row section keeps more than one row of a field, so only it can. A single-row section and
	a parent-section field hold exactly the one value on screen, and a More button there would open on
	itself. Read off the section brain, never off a list of section names kept here."""
	if not (section.is_multi_row and section.child_table_field):
		return False
	return len(doc.get(section.child_table_field) or []) > 1


def _field_values(doc, section, fieldname, value):
	"""Every value this field is kept under — one per row on a multi-row section, else the one on show.
	What `empty_everywhere` decides over; the panel still displays only `value`."""
	if not (section.is_multi_row and section.child_table_field):
		return [value]
	return [child.get(fieldname) for child in doc.get(section.child_table_field) or []]


def _value(doc, section, row):
	if section.child_table_field:
		child = _child_row(doc, section)
		return None if child is None else child.get(row.get("fieldname"))
	return doc.get(row.get("fieldname"))


def _key_value_sections():
	"""The sections whose rows are their own fields, read from the section brain rather than listed here."""
	return [
		frappe.get_cached_doc("CRM Lead Section", name)
		for name in frappe.get_all("CRM Lead Section", filters={"is_key_value": 1}, pluck="name")
	]


def _entitled_to_lead_grain(doc):
	"""Does the viewer's entitlement reach THIS lead's grain?

	A key-value section declares no catalogued field, so there is no per-field tick to resolve and
	`_select` never admits it — which is how these answers previously reached anyone holding read on the
	lead, ungated, while every named field beside them was grain-filtered. The section is admitted at
	grain level instead, through `taxonomy.grain.covers`: the one wildcard matcher, never a tuple lookup."""
	grains = entitlement.entitled_grains()
	if grains == entitlement.ALL_GRAINS:
		return True
	lead = (doc.get("custom_vertical") or "", doc.get("custom_group") or "",
	        doc.get("custom_current_program") or "")
	return any(
		grain.covers({"vertical": v, "group": g, "program": p}, *lead)
		for v, g, p in grains
	)


def _answers_by_question(doc, section):
	"""A key-value section's rows grouped by the question they answer, newest last."""
	grouped = {}
	for row in doc.get(section.child_table_field) or []:
		grouped.setdefault(row.get(section.row_key_field) or "", []).append(row)
	return grouped


def _screening_answers(doc, section):
	"""The latest answer to each question a key-value section holds, under the wording the patient saw.

	A question answered more than once — the same question on a later campaign, answered differently —
	keeps every answer, and this shows the newest through the SAME rule every multi-row consumer uses.
	`has_more` tells the panel to offer the history; the rest is read only, because a screening question
	is declared nowhere and there is no field to write an answer back through."""
	entries = []
	for identity, rows in _answers_by_question(doc, section).items():
		row = keyvalue.newest_first(rows)[0]
		value = row.get(section.value_field)
		has_more = len(rows) > 1
		entries.append({
			"field_key": f"{section.name}#{identity}",
			"label": row.get(section.label_field) or row.get(section.question_field) or _("(no question)"),
			"fieldname": "",
			"fieldtype": "Data",
			"options": "",
			"value": value,
			"display": None,
			"empty": empty_everywhere([r.get(section.value_field) for r in rows]),
			"read_only": True,
			"has_more": has_more,
			# After every catalogued field: an operator reads the named answers first, the backlog last.
			"_idx": 20_000,
		})
	return entries


def _display_label(df, value):
	"""The panel's label for a Link value. None for a non-Link field, which tells the panel to render
	the raw value."""
	if not (df and df.fieldtype == "Link" and df.options and value):
		return None
	return labels.title_of(df.options, value)


@frappe.whitelist()
def lead_detail(lead):
	"""Read projection: {sections:[{key,label,order,fields:[{field_key,label,fieldname,fieldtype,
	options,value,display,empty,has_more,read_only}]}]}. Permission-gated; values resolved server-side."""
	frappe.has_permission("CRM Lead", "read", doc=lead, throw=True)
	doc = frappe.get_doc("CRM Lead", lead)
	buckets = {}
	for fk, row in _select(doc).items():
		section = _section_of(row)
		bucket = buckets.setdefault(section.name, {"key": section.name, "label": section.title, "order": section.display_order, "fields": []})
		df = _docfield(section.target_doctype, row.get("fieldname"))
		value = _value(doc, section, row)
		has_more = _has_history(doc, section)
		bucket["fields"].append({
			"field_key": fk,
			"label": row.get("label") or row.get("fieldname"),
			"fieldname": row.get("fieldname"),
			"fieldtype": df.fieldtype if df else "Data",
			"options": (df.options or "") if df else "",
			"value": value,
			"display": _display_label(df, value),   # clean title_field label for Link/composite-PK values
			"empty": empty_everywhere(_field_values(doc, section, row.get("fieldname"), value)),
			"has_more": has_more,
			"read_only": _is_readonly(section, row.get("fieldname")),
			# order = the field's position in its target doctype (operator-controlled, not hardcoded)
			"_idx": df.idx if df else 10_000,
		})
	# A key-value section owns no catalogued field, so the loop above never opens a bucket for it: its
	# rows are the section. Opened here from the section itself, and only when the lead has answers.
	for section in _key_value_sections() if _entitled_to_lead_grain(doc) else []:
		answers = _screening_answers(doc, section)
		if not answers:
			continue
		bucket = buckets.setdefault(
			section.name,
			{"key": section.name, "label": section.title, "order": section.display_order, "fields": []},
		)
		bucket["fields"] += answers
	for b in buckets.values():
		b["fields"].sort(key=lambda f: (f.pop("_idx"), f["label"]))
	sections = sorted(buckets.values(), key=lambda s: s["order"])
	return {"sections": sections}


def _stage_write(doc, section, row, value):
	"""Stage one field write onto the in-memory doc (parent field or child row). Goes through the
	doc API only — never raw SQL."""
	table = section.child_table_field
	fieldname = row.get("fieldname")
	if not table:
		doc.set(fieldname, value)
		return
	child = _child_row(doc, section) or doc.append(table, {})
	child.set(fieldname, value)


def _entry(value, display, on, source):
	"""THE history entry shape, written once. Both branches return it, so the modal renders one thing
	however the rows behind it are kept: `on` is when the row is stamped, `source` where it came from
	(a key-value row names its form; a multi-row row is our own record and names nothing)."""
	return {"value": value, "display": display, "empty": _is_empty(value), "on": on, "source": source}


def _key_value_history(doc, section, identity):
	"""Every answer a lead has given to ONE question of a key-value section, newest first.

	Gated at GRAIN, not per field: a key-value section declares no catalogued field, so there is no tick
	to resolve — exactly the gate `lead_detail` opens these sections behind."""
	if not _entitled_to_lead_grain(doc):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	newest = keyvalue.newest_first(_answers_by_question(doc, section).get(identity) or [])
	return {
		"label": (newest[0].get(section.label_field) if newest else "") or _("(no question)"),
		"entries": [
			_entry(r.get(section.value_field), None, r.get("creation"), r.get("form"))
			for r in newest
		],
	}


def _multi_row_history(doc, field_key):
	"""Every row's value for ONE catalogued field of a multi-row section, newest first.

	Gated by `_select` — the panel's own field gate — so the modal can never answer a field the panel
	declined to show, and an out-of-grain or unentitled key is refused rather than quietly answered.
	Ordering is not invented here: `multirow.sorted_child_rows` is the same rule whose head the panel is
	already displaying. Routing comes off the catalog row's section Link, never off the parsed key."""
	row = _select(doc).get(cstr(field_key))
	if row is None:
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	section = _section_of(row)
	fieldname = row.get("fieldname") or ""
	df = _docfield(section.target_doctype, fieldname)
	children = multirow.sorted_child_rows(doc.get(section.child_table_field) or [], section.row_key_field)
	entries = []
	for child in children:
		value = child.get(fieldname)
		entries.append(_entry(value, _display_label(df, value), child.get(section.row_key_field), None))
	return {"label": row.get("label") or fieldname, "entries": entries}


@frappe.whitelist()
def section_history(lead, field_key):
	"""Everything behind one field of the panel, newest first — what the More button opens.

	`field_key` is the key the panel already handed the caller, so a reader can ask for nothing it was
	not shown. Gated on read of the LEAD, exactly as `lead_detail` is: a row hangs off a lead, and a lead
	outside the caller's line is unreadable, so its history is too.

	ONE parse, then the SECTION decides which history this is — never the separator the key happened to
	carry. A section that keeps one row of a field keeps no history, and says so instead of failing."""
	frappe.has_permission("CRM Lead", "read", doc=lead, throw=True)
	section_key, member = parse_field_key(field_key)
	if not frappe.db.exists("CRM Lead Section", section_key):
		frappe.throw(_("{0} keeps no history.").format(field_key), title=_("No history"))
	section = frappe.get_cached_doc("CRM Lead Section", section_key)
	doc = frappe.get_doc("CRM Lead", lead)
	if section.is_key_value:
		return _key_value_history(doc, section, member)
	if section.is_multi_row:
		return _multi_row_history(doc, field_key)
	frappe.throw(_("{0} keeps no history.").format(section.title), title=_("No history"))


@frappe.whitelist()
def update_lead_detail(lead, changes):
	"""Write path. `changes` is {field_key: value}. Only field_keys in the SERVER-BUILT writable
	projection (entitled ∧ not read-only ∧ not protected) are accepted; anything else is rejected.
	Persists via doc.save() so field perms, validate and doc_events all re-fire."""
	frappe.has_permission("CRM Lead", "write", doc=lead, throw=True)
	changes = frappe.parse_json(changes) if isinstance(changes, str) else (changes or {})
	if not isinstance(changes, dict):
		frappe.throw(_("Invalid changes payload"))
	doc = frappe.get_doc("CRM Lead", lead)
	selected = _select(doc)
	writable = writable_keys(selected, _is_readonly)
	for fk, value in changes.items():
		if fk not in writable:
			frappe.throw(_("Field {0} is not editable here").format(fk))
		row = selected[fk]
		_stage_write(doc, _section_of(row), row, value)
	doc.save()
	return {"ok": True}
