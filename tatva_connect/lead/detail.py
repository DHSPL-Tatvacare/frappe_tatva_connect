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
  * Read is permission-gated through `access.posture.require` — the ONE checkpoint, which asks the
    engine on an ordinary Desk request and stands aside inside a server-opened trusted block (the
    partner API, already gated by its mapping and grain; it reaches this projection through
    `activity.api.lead_field_values`). The ENTITLEMENT gate below is untouched by the posture: a
    trusted caller still sees only the fields its own grain's contract ticks.
  * Values are resolved server-side (no client SQL, no field enumeration) — there is NO string-built
    SQL in this module; everything flows through the frappe doc API.
  * Write goes through a SERVER-BUILT allowlist (entitled ∧ not read-only ∧ not a protected
    routing field). A field_key outside it is rejected — a crafted payload can never reach
    routing/owner/out-of-grain/read-only fields (no mass-assignment).
"""
import re

import frappe
from frappe import _
from frappe.model import NO_VALUE_FIELDS
from frappe.utils import cint, cstr

from tatva_connect.access import entitlement, posture
from tatva_connect.lead import keyvalue, multi_value, multirow
from tatva_connect.taxonomy import grain, labels, picklist

# Identity/routing fields: shown (informative) but NEVER editable on this panel. Identity
# (vertical/group) is the dedup anchor; program transitions happen via deliberate routing
# flows, not a casual field edit. Forced read-only regardless of catalog/property-setter state.
_PROTECTED_FIELDS = frozenset({"custom_vertical", "custom_group", "custom_current_program"})

_CATALOG_FIELDS = [
	"field_key", "label", "fieldname", "section", "is_multi_value",
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


def _is_readonly(section, fieldname, is_multi_value=False, new_observation=False):
	"""Read-only iff a protected routing field, the row key of a row being EDITED, the docfield says so,
	or the field is unknown (fail-closed: an unresolvable field is never writable).

	A multi-row section's row key is the row's ADDRESS, not a value on it: editing it re-keys the row, so
	the next write naming the original key appends a duplicate instead of updating. Read off the section
	that declares it, never restated here — a single-row section declares none and loses nothing.

	BUT A NEW OBSERVATION HAS NO ADDRESS YET. `_stage_section` appends a fresh row for one, so there is
	nothing to re-key and the danger above cannot arise — while refusing it there means a row is born with
	a BLANK key, which no later write can ever target. That is not hypothetical: `Plan retool` asks the rep
	for `Assign plan Date Time` and stars it, because that stamp IS how a plan row gets its identity, and
	locking it turned the form into one nobody could submit.

	A multi-value field has no column on the section's doctype ON PURPOSE, so the fail-closed branch is
	not the right answer for it: what makes it writable is the catalog tick, and that is asked here."""
	if fieldname in _PROTECTED_FIELDS:
		return True
	if fieldname == (section.row_key_field or "") and not new_observation:
		return True
	if is_multi_value:
		return False
	df = _docfield(section.target_doctype, fieldname)
	return bool(df.read_only) if df else True


def writable_keys(selected, is_readonly, new_observation=False):
	"""The field_keys writable through update_lead_detail: those whose target field is not read-only.
	Target doctype is read off the section brain; `is_readonly(target_doctype, fieldname)` is injected.

	`new_observation` rides through because WHAT THE WRITE IS decides whether a row key may be set — see
	`_is_readonly`. The Data tab edits the row it is showing and passes False; an activity punch is a fresh
	observation and passes True."""
	out = set()
	for fk, row in selected.items():
		if not is_readonly(_section_of(row), row.get("fieldname") or "",
		                   cint(row.get("is_multi_value")), new_observation):
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
	"""The single child row a child-section field reads from — `multirow.row_for_section`, the ONE rule
	the Data tab, the headline sync and a workflow criterion all read through."""
	return multirow.row_for_section(doc, section)


def _is_multi_row(section):
	"""Is this section a child table that keeps MANY rows? A key-value section keeps many rows too, but
	its row IS its field, so it is not one of these — its detail is a question's answers, not a table."""
	return bool(section.is_multi_row and section.child_table_field and not section.is_key_value)


def _bucket(doc, section):
	"""The section envelope the panel renders into. `multi_row` + `row_count` are facts about the
	SECTION, not about any field on it: a child table keeping three rows keeps three rows once, and the
	detail behind it is the TABLE. Attaching that fact per field is what put a More button on all 17 lab
	measurements, each opening one column of the same three rows."""
	return {
		"key": section.name,
		"label": section.title,
		"order": section.display_order,
		"multi_row": _is_multi_row(section),
		"row_key": section.row_key_field or "",
		# The child doctype the rows modal's Filter/SortBy/ColumnSettings are about — read off the
		# section brain and handed over, so the client never resolves a doctype from a section key.
		"doctype": section.target_doctype or "",
		"row_count": len(doc.get(section.child_table_field) or []) if section.child_table_field else 0,
		"fields": [],
	}


def _row_key(doc, section):
	"""The address the row ON SHOW is kept under — the section's own row key, read off that row.

	ONE rule, read AND write: whatever row the panel flattened to is the row an edit lands on, so a
	multi-value selection can never be read from one cycle and written onto another. Blank on a section
	that declares no row key, and blank is a real address, not an unset one."""
	if not section.child_table_field:
		return ""
	child = _child_row(doc, section)
	return cstr(child.get(section.row_key_field)) if (child and section.row_key_field) else ""


def _field_values(doc, section, fieldname, value):
	"""Every value this field is kept under — one per row on a multi-row section, else the one on show.
	What `empty_everywhere` decides over; the panel still displays only `value`."""
	if not (section.is_multi_row and section.child_table_field):
		return [value]
	return [child.get(fieldname) for child in doc.get(section.child_table_field) or []]


def _multi_values(doc, section, row):
	"""Every address this multi-value field holds selections at — the values on show first.

	One per row of a multi-row section, so `empty_everywhere` reads the field the same way it reads a
	column: blank on the latest cycle but answered on an earlier one is not an empty field."""
	held = multi_value.read_all(doc)
	field_key = row.get("field_key")
	if not (section.is_multi_row and section.child_table_field):
		return [held.get((field_key, _row_key(doc, section)), [])]
	return [held.get((field_key, cstr(child.get(section.row_key_field))), [])
	        for child in doc.get(section.child_table_field) or []]


def _value(doc, section, row):
	if cint(row.get("is_multi_value")):
		return multi_value.read(doc, row.get("field_key"), _row_key(doc, section))
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
			# The identity IS the address, so an activity form can name a question the way it names any other field.
			"fieldname": identity,
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
	the raw value.

	A multi-value field holds a LIST of the same Link, so its display is the list of those labels —
	`labels.label` falls back to the raw value, so one unresolvable selection never blanks the rest."""
	if not (df and df.fieldtype == "Link" and df.options and value):
		return None
	if isinstance(value, list):
		return [labels.label(v, df.options) for v in value]
	return labels.title_of(df.options, value)


def _link_query(df, fieldname, lead):
	"""The scoped query a picklist picker must ask, or None for any other Link (which keeps the native
	one). The framework's `search_link` reads the master through the generic list path, so it demands
	DocType read on CRM Picklist Value — which no rep holds, and every picklist picker 403s. This names
	`taxonomy.picklist.picklist_query` instead: whitelisted, grain-derived server-side, written for
	exactly this. The category comes from `picklist.category_of` — the ONE rule, never re-derived here
	and never in the client. Grain is NOT passed: the query re-reads it off the lead, and an exact axis
	filter would drop every blank-axis global option.

	The fieldname is the CATALOG's, not the docfield's: a multi-value field is stored in a column called
	`value` and its own name — the one the category is derived from — lives on the catalog row alone."""
	if not (df and df.fieldtype == "Link" and df.options == "CRM Picklist Value"):
		return None
	return {
		"query": "tatva_connect.taxonomy.picklist.picklist_query",
		"filters": {"category": picklist.category_of(fieldname), "lead": lead},
	}


# The record types whose Data tab this projection serves; a Deal is served its lead's panel, never its own.
_PANEL_PARENTS = ("CRM Lead", "CRM Deal")


def resolve_panel_lead(record, doctype="CRM Lead"):
	"""The CRM Lead a panel is really about — ONE hop, at the entry point, and nothing below it changes.

	A Deal is the customer a Lead became, so its Data tab is the LEAD's sections read through `deal.lead`;
	nothing is copied onto the deal. The hop is a plain field read, deliberately ungated: the gate is
	`posture.require` on the RESOLVED lead, so reaching a patient through a deal id can never show more
	than reaching them through the lead id would."""
	if doctype not in _PANEL_PARENTS:
		frappe.throw(_("Unsupported record type {0}").format(doctype))
	if doctype == "CRM Lead":
		return record
	lead = frappe.db.get_value("CRM Deal", record, "lead")
	if not lead:
		frappe.throw(_("This deal is not linked to a lead."), title=_("No lead on this deal"))
	return lead


@frappe.whitelist()
def lead_detail(lead, doctype="CRM Lead"):
	"""Read projection: {sections:[{key,label,order,multi_row,row_key,row_count,fields:[{field_key,label,
	fieldname,fieldtype,options,value,display,empty,read_only}]}]}. A key-value entry also carries
	`has_more` — its own answers, and a picklist Link a `link_query` — the scoped query its picker asks.
	A multi-value field carries `multi_value` and answers in LISTS (`value` and `display` both), which is
	the panel's signal to render a set of selections rather than one.
	Permission-gated; values resolved server-side.

	`doctype` names the RECORD the caller is looking at, not a field's target: a Deal resolves to its lead
	here and the rest of this module never learns a second doctype. The answer carries the resolved `lead`
	back so the panel's own follow-up reads (history, a section's rows) address the lead, and `read_only`,
	because `update_lead_detail`'s allowlist is built for a lead and a write arriving from a deal surface
	is not the same question."""
	lead = resolve_panel_lead(lead, doctype)
	posture.require("CRM Lead", "read", doc=lead)
	doc = frappe.get_doc("CRM Lead", lead)
	buckets = {}
	for fk, row in _select(doc).items():
		section = _section_of(row)
		bucket = buckets.setdefault(section.name, _bucket(doc, section))
		is_multi = cint(row.get("is_multi_value"))
		# A multi-value field has no column on its section, so what it IS reads off the column its selections really live in.
		df = multi_value.value_field() if is_multi else _docfield(section.target_doctype, row.get("fieldname"))
		value = _value(doc, section, row)
		link_query = _link_query(df, row.get("fieldname"), lead)
		bucket["fields"].append({
			"field_key": fk,
			"label": row.get("label") or row.get("fieldname"),
			"fieldname": row.get("fieldname"),
			"fieldtype": df.fieldtype if df else "Data",
			"options": (df.options or "") if df else "",
			"value": value,
			"display": _display_label(df, value),   # clean title_field label for Link/composite-PK values
			"empty": empty_everywhere(
				_multi_values(doc, section, row) if is_multi
				else _field_values(doc, section, row.get("fieldname"), value)
			),
			"read_only": _is_readonly(section, row.get("fieldname"), is_multi),
			# order = the field's position in its target doctype (operator-controlled); a multi-value field holds none, so it lands where an unknown one does
			"_idx": 10_000 if is_multi else (df.idx if df else 10_000),
			# only a picklist Link carries one; every other Link keeps the framework's own picker
			**({"link_query": link_query} if link_query else {}),
			**({"multi_value": True} if is_multi else {}),
		})
	# A key-value section owns no catalogued field, so the loop above never opens a bucket for it: its
	# rows are the section. Opened here from the section itself, and only when the lead has answers.
	for section in _key_value_sections() if _entitled_to_lead_grain(doc) else []:
		answers = _screening_answers(doc, section)
		if not answers:
			continue
		bucket = buckets.setdefault(section.name, _bucket(doc, section))
		bucket["fields"] += answers
	for b in buckets.values():
		b["fields"].sort(key=lambda f: (f.pop("_idx"), f["label"]))
	sections = sorted(buckets.values(), key=lambda s: s["order"])
	return {"sections": sections, "lead": lead, "read_only": doctype != "CRM Lead"}


def _stage_section(doc, section, staged, new_observation=False):
	"""Stage every write to ONE section onto ONE row, choosing that row once.

	A multi-value field keeps its own address and is staged individually — the row it belongs to is its
	own (`_row_key`), not this one."""
	table = section.child_table_field
	child = None
	if table and any(not cint(r.get("is_multi_value")) for r, _v in staged):
		fresh = new_observation and _is_multi_row(section)
		child = doc.append(table, {}) if fresh else (_child_row(doc, section) or doc.append(table, {}))
	for row, value in staged:
		if cint(row.get("is_multi_value")):
			multi_value.replace(doc, row.get("field_key"), _row_key(doc, section), value or [])
		elif not table:
			doc.set(row.get("fieldname"), value)
		else:
			child.set(row.get("fieldname"), value)


def _entry(value, display, on, source):
	"""THE history entry shape: `on` is when the row is stamped, `source` where it came from (a
	key-value row names the form the patient answered on)."""
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


@frappe.whitelist()
def section_history(lead, field_key):
	"""Every answer a lead has given to ONE key-value question, newest first — what its More opens.

	`field_key` is the key the panel already handed the caller, so a reader can ask for nothing it was
	not shown. Gated on read of the LEAD, exactly as `lead_detail` is: a row hangs off a lead, and a lead
	outside the caller's line is unreadable, so its answers are too.

	ONE parse, then the SECTION decides — never the separator the key happened to carry. Only a
	key-value section answers here: its row IS its field, so a field's history and a row's history are
	the same list. A multi-row section is a TABLE and its detail is `lead_detail_rows`; asking for one
	column of it per field is what put a More button on every measurement."""
	posture.require("CRM Lead", "read", doc=lead)
	section_key, member = parse_field_key(field_key)
	if not frappe.db.exists("CRM Lead Section", section_key):
		frappe.throw(_("{0} keeps no history.").format(field_key), title=_("No history"))
	section = frappe.get_cached_doc("CRM Lead Section", section_key)
	doc = frappe.get_doc("CRM Lead", lead)
	if section.is_key_value:
		return _key_value_history(doc, section, member)
	frappe.throw(_("{0} keeps no history.").format(section.title), title=_("No history"))


# ------------------------------- the section's own rows -------------------------------
# A section IS a child table, so the detail behind it is that table: every column, one line per row.
_ROWS_PAGE_MAX = 200
# The count field every paged list in this app asks for (`crm/api/doc.py:17`, used for `total_count` on
# each list and for the kanban column counts). Same call shape, declared here because the dependency
# runs the other way — the fork imports from this app, never this app from the fork.
_COUNT_FIELD = {"COUNT": "name", "as": "total_count"}
# What a free-text search can honestly match. A leading-wildcard LIKE on a Float or a Date matches by
# coincidence of digits, so text is searched and measurements are not — read off fieldtype, decided once.
_SEARCHABLE_FIELDTYPES = frozenset({"Data", "Small Text", "Text", "Long Text", "Text Editor", "Select",
                                    "Link", "Dynamic Link", "Read Only"})


def _row_columns(section):
	"""Every column of a section's child table — row key first, then the doctype's own field order.

	Derived from the child doctype's meta, never a list kept here: a section that grows a field grows a
	column, and 26 today or 58 later is the same code. What carries no cell is frappe's own answer
	(`NO_VALUE_FIELDS` — the layout breaks and the nested tables), not a set restated here. The row key
	leads because it is what a reader scans down.

	Every column is served AND shown: the table is the table. Which of them a reader keeps on screen is
	the column picker's business, exactly as on a listing page — not a second opinion taken here."""
	meta = frappe.get_meta(section.target_doctype)
	key = cstr(section.row_key_field)
	fields = [df for df in meta.fields if df.fieldtype not in NO_VALUE_FIELDS and not df.hidden]
	fields.sort(key=lambda df: (0 if df.fieldname == key else 1, df.idx))
	return [
		{"key": df.fieldname, "label": _(df.label or df.fieldname), "fieldtype": df.fieldtype,
		 "options": df.options or ""}
		for df in fields
	]


def _filled_columns(section, columns, owned):
	"""Which columns hold a value anywhere in THIS lead's rows, in one aggregate query — no rows read.

	D5: never offer a sort on a column the data does not populate, or it orders by the tiebreaker while
	looking authoritative. This is a fact about the data, which only the server can know; it decides what
	is SORTABLE, never what a reader may look at (the picker owns that, and it is offered every column).
	`COUNT(col)` counts non-NULL, so a stored 0 counts as filled — the same answer `_is_empty` gives."""
	if not columns:
		return set()
	counts = frappe.get_all(
		section.target_doctype,
		fields=[{"COUNT": col["key"], "as": col["key"]} for col in columns],
		parent_doctype="CRM Lead",
		filters=owned,
	)
	filled = counts[0] if counts else {}
	return {col["key"] for col in columns if cint(filled.get(col["key"]))}


def _multi_value_columns(section):
	"""The section's multi-value fields as columns of its rows table — appended, never SQL.

	They are not columns of the child doctype (that is what makes them multi-value), so they can never
	reach a WHERE, an ORDER BY or a `COUNT(col)`: `_row_columns` stays the SQL list and these ride
	alongside it. Label and key come from the catalog, the type from the column the selections live in,
	so the table names the field exactly as the panel above it does."""
	rows = frappe.get_all(
		"CRM Lead API Field", filters={"section": section.name, "is_multi_value": 1},
		fields=["field_key", "label", "fieldname"], order_by="field_key asc",
	)
	df = multi_value.value_field()
	return [{"key": r["fieldname"], "field_key": r["field_key"], "label": _(r["label"] or r["fieldname"]),
	         "fieldtype": df.fieldtype, "options": df.options or ""} for r in rows]


def _row_cells(child, columns, extra=(), held=None, row_key_field=""):
	"""One child row as {column_key: value}, a Link resolved to the SAME label the panel shows.

	A multi-value column has nothing on the row to read, so its cell is read from the lead's own
	selections at THIS row's address — the same address the panel flattens to for the latest row, so
	the first line of the table and the panel behind it can never say different things."""
	cells = {"name": child.get("name")}
	for col in columns:
		value = child.get(col["key"])
		if col["fieldtype"] == "Link" and col["options"]:
			value = labels.title_of(col["options"], value) or value
		cells[col["key"]] = value
	address = cstr(child.get(row_key_field)) if row_key_field else ""
	for col in extra:
		selected = (held or {}).get((col["field_key"], address), [])
		cells[col["key"]] = [labels.label(v, col["options"]) for v in selected]
	return cells


def _sections_on_panel(selected):
	"""The section keys this caller's panel opens for this lead — the ONE gate the rows reader reuses,
	so a section the panel declined to show can never be read around it. Takes the ALREADY-resolved
	selection: `_select` reads the whole catalog and resolves entitlement per row, so it is asked once
	per request and passed, never called again for a second question about the same answer."""
	return {cstr(row.get("section")) for row in selected.values()}


def _row_order_by(section, columns, order_by):
	"""The table's ordering: `multirow`'s one rule by default, or a caller's column — as an ALLOWLIST
	over the served columns, never a passthrough (D3). `SortBy` emits "field dir, …"; the first wins."""
	first = cstr(order_by or "").split(",")[0].strip()
	if not first:
		return multirow.order_by(section.row_key_field)
	field, _sep, direction = first.partition(" ")
	direction = (direction.strip() or "desc").lower()
	if field not in {c["key"] for c in columns} or direction not in ("asc", "desc"):
		frappe.throw(_("Cannot sort by {0}").format(first))
	return f"{field} {direction}"


def _row_filters(columns, filters):
	"""A caller's filters, narrowed to the served columns. `Filter` emits frappe's own filter dict, so
	it is handed to the query untouched — the framework parses the operators, we only gate the field."""
	parsed = frappe.parse_json(filters) if isinstance(filters, str) else (filters or {})
	if not isinstance(parsed, dict):
		frappe.throw(_("Invalid filters payload"))
	allowed = {c["key"] for c in columns}
	for field in parsed:
		if field not in allowed:
			frappe.throw(_("Cannot filter by {0}").format(field))
	return parsed


def _row_search(columns, search):
	"""Free text across the TEXT columns — `or_filters`, because frappe ANDs `filters` (D2). A Float or a
	Date is not searched: `%7%` would match a triglyceride of 178 and read as a hit on nothing."""
	needle = cstr(search or "").strip()
	if not needle:
		return {}
	return {c["key"]: ["like", f"%{needle}%"] for c in columns
	        if c["fieldtype"] in _SEARCHABLE_FIELDTYPES}


@frappe.whitelist()
def lead_detail_rows(lead, section, search=None, filters=None, order_by=None,
                     page_length=20, page_length_count=20):
	"""Every row of ONE child-table section of a lead — what the section's `View more` opens.

	The Leads list contract, copied not adapted (C1/C2): `page_length` is the WINDOW (Load More refetches
	0..N with a bigger one), `page_length_count` the page SIZE the footer picks, and the envelope answers
	in `data`/`page_length`/`page_length_count`/`total_count`/`row_count` — the same keys `crm/api/doc.py`
	answers every list in. `label`, `row_key` and `columns` ride alongside, as that envelope's own extras do.

	The rows are read through frappe's OWN query on the child doctype (`parent_doctype="CRM Lead"`), so
	filtering, sorting and paging are the framework's, not a second engine written here. Default ordering
	is `multirow.order_by` — the same rule whose head the panel is flattening to, so the first line of the
	table is the line on the panel behind it.

	Two gates, both already owned elsewhere: read of the LEAD (as `lead_detail`), and the section being
	one the caller's panel opens (`_select`) — a grain-foreign section answers nothing. Within it a
	caller may sort or filter only by a served column; both are allowlisted, never passed through.

	A multi-value field is a column of this table too — it is what the reader was just looking at on the
	panel — read per row at that row's own address. It is served `sortable: 0, filterable: 0`, because
	it is not addressable in SQL."""
	posture.require("CRM Lead", "read", doc=lead)
	doc = frappe.get_doc("CRM Lead", lead)
	if cstr(section) not in _sections_on_panel(_select(doc)):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	sec = frappe.get_cached_doc("CRM Lead Section", section)
	if not sec.child_table_field:
		frappe.throw(_("{0} keeps no rows.").format(sec.title), title=_("No rows"))
	columns = _row_columns(sec)
	# Shown but never queried: SQL cannot address a multi-value column, so it stays out of the select, both allowlists and the filled count, and is read per row from the lead's own selections.
	extra = _multi_value_columns(sec)
	owned = {"parent": lead, "parenttype": "CRM Lead", "parentfield": sec.child_table_field}
	narrowed = {**owned, **_row_filters(columns, filters)}
	query = {"parent_doctype": "CRM Lead", "filters": narrowed, "or_filters": _row_search(columns, search)}
	window = min(cint(page_length) or 20, _ROWS_PAGE_MAX)
	children = frappe.get_all(
		sec.target_doctype,
		fields=["name"] + [c["key"] for c in columns],
		order_by=_row_order_by(sec, columns, order_by),
		limit=window,
		**query,
	)
	# The count carries the SAME narrowing as the page (C7), so "3 of 12" can never contradict it —
	# counted in the DB, never by reading the whole matching set back to take its length.
	counted = frappe.get_all(sec.target_doctype, fields=[_COUNT_FIELD], **query)
	filled = _filled_columns(sec, columns, owned)
	held = multi_value.read_all(doc) if extra else {}
	return {
		"label": sec.title,
		"row_key": sec.row_key_field or "",
		# `sortable` answers D5 and nothing else: every column is still offered to the picker and to Filter.
		# `sortable`/`filterable` answer D5 and D1 — a column SQL cannot address reaches neither ORDER BY nor WHERE, though the column picker still offers it; `field_key` is how the cell reader addresses the selections.
		"columns": [{**c, "sortable": c["key"] in filled, "filterable": True} for c in columns]
		           + [{**{k: v for k, v in c.items() if k != "field_key"},
		               "sortable": False, "filterable": False} for c in extra],
		"data": [_row_cells(child, columns, extra, held, sec.row_key_field or "")
		         for child in children],
		"page_length": window,
		"page_length_count": cint(page_length_count) or 20,
		"row_count": len(children),
		"total_count": cint(counted[0].get("total_count")) if counted else 0,
	}


@frappe.whitelist()
def update_lead_detail(lead, changes, new_observation=False):
	"""Write path. `changes` is {field_key: value}. Only field_keys in the SERVER-BUILT writable
	projection (entitled ∧ not read-only ∧ not protected) are accepted; anything else is rejected.
	Persists via doc.save() so field perms, validate and doc_events all re-fire.

	`new_observation` says WHAT this write is, and only a MULTI-ROW section can tell the difference. The
	Data tab edits the row it is showing — the latest, the same one every consumer flattens to — so it
	leaves this False. An activity form's answers are a fresh observation with no row in hand, so they land
	on a NEW row: a second payment is a second purchase, not an edit of the first. 30% of paying patients
	have more than one payment, and updating the latest silently destroyed the earlier one."""
	posture.require("CRM Lead", "write", doc=lead)
	changes = frappe.parse_json(changes) if isinstance(changes, str) else (changes or {})
	if not isinstance(changes, dict):
		frappe.throw(_("Invalid changes payload"))
	doc = frappe.get_doc("CRM Lead", lead)
	selected = _select(doc)
	writable = writable_keys(selected, _is_readonly, new_observation)
	for fk in changes:
		if fk not in writable:
			frappe.throw(_("Field {0} is not editable here").format(fk))
	# Grouped by SECTION because a row is chosen once per write and not once per field: a payment id and
	# its amount arriving together belong on ONE row, and staging them separately made two.
	staged = {}
	for fk, value in changes.items():
		row = selected[fk]
		section = _section_of(row)
		staged.setdefault(section.name, (section, []))[1].append((row, value))
	for section, rows in staged.values():
		_stage_section(doc, section, rows, new_observation)
	doc.save()
	return {"ok": True}


def write_lead_fields(lead, values, new_observation=True):
	"""`update_lead_detail` addressed by FIELDNAME — what an activity form's `source = Lead` answers write through. Address translation only: the gate, the staging and the save stay `update_lead_detail`'s."""
	if not values:
		return {}
	selected = _select(frappe.get_doc("CRM Lead", lead))
	changes = {}
	for fieldname, value in values.items():
		found = [fk for fk, row in selected.items() if (row.get("fieldname") or "") == fieldname]
		if len(found) != 1:
			# None means no section declares it here; more than one means the fieldname names no single row.
			frappe.throw(_("Field {0} has no single home on this lead").format(fieldname))
		changes[found[0]] = value
	return update_lead_detail(lead, changes, new_observation=new_observation)
