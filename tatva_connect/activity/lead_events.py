"""How a record's own history becomes a rail line — ONE declaration, three readers.

A field edit IS a record: the `Version` row frappe writes for the save. The rail indexes that row like any
other source, so one save is one event and a reader can page back past frappe's ten-version window. What a
save SHOWS is `rail_changes` below — asked by the index writer to decide whether to index, and by the rail
to decide what to draw, so a pointer exists exactly when a row renders.

WHY IT LIVES HERE. The rule was written inline and byte-identical TWICE in the crm fork
(`get_lead_activities` and `get_deal_activities`). The rail needs the same lines without paying for
`get_docinfo` — 0.46 s and 178 queries on the fattest dev lead, because that call also loads assignments,
likes, energy points and attachments the rail never reads. Copying the rule a third time would grow a
second brain, so it moves here and both fork readers delegate to it in one line.

PARITY IS THE POINT. `VERSION_WINDOW` and the `track_changes` guard are frappe's own
(`desk/form/load.py:get_versions`), reproduced here so the fast path shows exactly the history the
dormant path shows — no more, no less. The one deliberate divergence is noted on `field_changes`.
"""

import frappe
from frappe import _
from frappe.translate import get_translated_doctypes
from frappe.utils import cint, cstr
from frappe.utils.caching import request_cache

from tatva_connect.lead import field_value, keyvalue, multi_value, multirow
from tatva_connect.partner_api.doctype.crm_lead_section import crm_lead_section

# Frappe shows a record's ten most recent edits and no further back (get_versions). The rail inherits
# that window rather than choosing its own, or the two paths would disagree about how far history goes.
VERSION_WINDOW = 10

# Fields whose edits are noise on a timeline — SLA bookkeeping, and the link each doctype is defined by.
_AVOID_FIELDS = {
	"CRM Lead": ("converted", "response_by", "sla_creation", "sla", "first_response_time", "first_responded_on"),
	"CRM Deal": ("lead", "response_by", "sla_creation", "sla", "first_response_time", "first_responded_on"),
}

_CREATION_TEXT = {"CRM Lead": "created this lead", "CRM Deal": "created this deal"}

# Derived columns a reader must never be shown: custom_stage follows custom_substage — the same move written twice.
NOISE_FIELDS = {"custom_stage"}


def rail_changes(doctype: str, version) -> list:
	"""The changes ONE save actually shows, or []. The index writer asks it whether to index a Version and
	the rail asks it what to draw, so a pointer exists exactly when a row renders and a page is never short."""
	return [
		c for c in field_changes(doctype, [version], doctype == "CRM Lead")
		if (c.get("data") or {}).get("field") not in NOISE_FIELDS
	]


def recent_versions(doctype: str, name: str) -> list:
	"""The edits a reader may see — frappe's own window, asked of the Version table directly.

	`get_docinfo` answers the same question, but it answers nine others at the same time. One query here."""
	if not frappe.get_meta(doctype).track_changes:
		return []
	return frappe.get_all(
		"Version",
		filters={"ref_doctype": doctype, "docname": str(name)},
		fields=["name", "owner", "creation", "data"],
		limit=VERSION_WINDOW,
		order_by="creation desc",
	)


def field_changes(doctype: str, versions: list, is_lead: bool) -> list:
	"""Version rows to timeline lines, in the order given — a save's own fields and its child rows through ONE line builder."""
	meta = frappe.get_meta(doctype)
	avoid = _AVOID_FIELDS.get(doctype, ())
	out = []
	for version in versions:
		diff = frappe.parse_json(version.data)
		entries = []
		for fieldname, old, new in diff.get("changed") or []:
			df = meta.get_field(fieldname)
			if df and fieldname not in avoid:
				entries.append((fieldname, df.label or fieldname, df, _as_saved(df, old), _as_saved(df, new)))
		entries += _row_entries(diff)
		out += [_line(version, is_lead, *e) for e in entries if e[3] or e[4]]
	return out


def _line(version, is_lead, field, label, df, old, new):
	"""One change as a timeline line — THE shape every renderer draws, for a lead field and a child row alike."""
	# Imported inside the call: crm's module imports this one, so a module-level import is a cycle.
	from crm.api.activities import ATTACHMENT_FIELDTYPES, attachment_label

	# An Attach value is a file_url — show the file's name, never our storage URL (M3).
	if df.fieldtype in ATTACHMENT_FIELDTYPES:
		old, new = (attachment_label(v) if v else v for v in (old, new))
	if (new or old) and df.options in _translated_doctypes():
		old, new = (_(v) if v else v for v in (old, new))
	activity_type = "changed" if old and new else "added" if new else "removed"
	data = {"field": field, "field_label": label, "value": new or old}
	if activity_type == "changed":
		data["old_value"] = old
	return {"activity_type": activity_type, "creation": version.creation, "owner": version.owner,
			"data": data, "is_lead": is_lead, "options": df.options or None}


def _as_saved(df, value):
	"""A value off frappe's diff as a reader sees it: a Link frappe did not title (`show_title_field_in_link`) is titled like a child row's."""
	if value and df.fieldtype == "Link" and df.options and not frappe.get_meta(df.options).show_title_field_in_link:
		return field_value.display(df, value) or value
	return value


@request_cache
def _translated_doctypes():
	"""frappe's translated doctypes, read once per request — `get_translated_doctypes` is two queries, and a rail asks it per line."""
	return frozenset(get_translated_doctypes())


def _row_entries(diff):
	"""(field, label, df, old, new) per child row a save touched; raw added/removed values read through `field_value.as_text`, edits arrive formatted by frappe."""
	by_section = {}
	for kind in ("added", "removed"):
		for cf, row in diff.get(kind) or []:
			by_section.setdefault(cf, {"added": [], "removed": [], "edited": []})[kind].append(frappe._dict(row))
	for cf, _idx, row_name, changed in diff.get("row_changed") or []:
		by_section.setdefault(cf, {"added": [], "removed": [], "edited": []})["edited"].append((row_name, changed))
	entries = []
	for cf, rows in by_section.items():
		if cf == multi_value.TABLE:
			entries += _selection_entries(rows["added"], rows["removed"])
			continue
		section = crm_lead_section.section_for_child(cf)
		if section:
			read = _answer_entries if section.is_key_value else _column_entries
			entries += read(section, rows)
	return entries


def _column_entries(section, rows):
	"""A section row's changes, one per column the Data tab shows (`detail.row_columns`)."""
	from tatva_connect.lead.detail import row_columns

	meta = frappe.get_meta(section.target_doctype)
	columns = {c["key"]: f"{_(section.title)} · {c['label']}" for c in row_columns(section)}
	out = []
	for came, kind in ((True, "added"), (False, "removed")):
		for row in rows[kind]:
			for key, label in columns.items():
				df = meta.get_field(key)
				if not multirow.is_blank(row.get(key), df.fieldtype):
					out.append((f"{section.child_table_field}.{key}", label, df, *_whole(field_value.as_text(df, row.get(key)), came)))
	for _name, changed in rows["edited"]:
		out += [(f"{section.child_table_field}.{fn}", columns[fn], meta.get_field(fn),
				 _as_saved(meta.get_field(fn), old), _as_saved(meta.get_field(fn), new))
				for fn, old, new in changed if fn in columns]
	return out


def _answer_entries(section, rows):
	"""A key-value section's changes, one per answer, labelled by the question it answers — as the Data tab shows it."""
	df = frappe.get_meta(section.target_doctype).get_field(section.value_field)
	field = f"{section.child_table_field}.{section.value_field}"
	label = lambda question: f"{_(section.title)} · {question}"  # noqa: E731
	question = lambda row: row.get(section.label_field) or row.get(section.question_field)  # noqa: E731
	earlier = _earlier_answers(section, rows["added"])
	out = []
	for came, kind in ((True, "added"), (False, "removed")):
		for r in rows[kind]:
			if not multirow.is_blank(r.get(section.value_field), df.fieldtype):
				old, new = _whole(field_value.as_text(df, r.get(section.value_field)), came)
				out.append((field, label(question(r)), df, field_value.as_text(df, earlier.get(r.name)) if came else old, new))
	edited = [(name, old, new) for name, changed in rows["edited"] for fn, old, new in changed if fn == section.value_field]
	if edited:
		asked = dict(frappe.get_all(section.target_doctype, filters={"name": ["in", [e[0] for e in edited]]},
									fields=["name", section.label_field], as_list=True))
		out += [(field, label(asked.get(name) or _(df.label)), df, old, new) for name, old, new in edited]
	return out


def _earlier_answers(section, added):
	"""{added row name: its question's previous answer} — a changed answer is appended, so "before" is `keyvalue.newest_first` below its `idx`."""
	key = section.row_key_field
	rows = [r for r in added if r.get(key)]
	if not rows:
		return {}
	held = frappe.get_all(
		section.target_doctype,
		filters={"parent": rows[0].parent, "parentfield": section.child_table_field, key: ["in", [r.get(key) for r in rows]]},
		fields=[key, section.value_field, "idx"],
	)
	out = {}
	for r in rows:
		before = keyvalue.newest_first([h for h in held if h[key] == r.get(key) and cint(h.idx) < cint(r.idx)])
		out[r.name] = before[0][section.value_field] if before else None
	return out


def _selection_entries(added, removed):
	"""Multi-value picks that really came or went. `multi_value.replace` re-adds the whole set, so a kept pick is no change."""
	df = multi_value.value_field()
	address = lambda r: (r.field_key, cstr(r.row_key), r.value)  # noqa: E731
	came, gone = {address(r) for r in added}, {address(r) for r in removed}
	out = []
	for picks, is_new in ((came - gone, True), (gone - came, False)):
		for field_key, _row_key, value in picks:
			label, fieldname = frappe.get_cached_value("CRM Lead API Field", field_key, ["label", "fieldname"]) or (None, None)
			out.append((f"{multi_value.TABLE}.{field_key}", _(label or fieldname or field_key), df,
						*_whole(field_value.as_text(df, value), is_new)))
	return out


def _whole(text, came):
	"""(old, new) for a value a row brought or took away with it."""
	return (None, text) if came else (text, None)


def creation_event(doctype: str, name: str) -> dict:
	"""The first line every timeline ends on — the record being created."""
	created, owner = frappe.db.get_value(doctype, name, ["creation", "owner"]) or (None, None)
	return {
		"activity_type": "creation",
		"creation": created,
		"owner": owner,
		"data": _(_CREATION_TEXT.get(doctype, "created this record")),
		"is_lead": doctype == "CRM Lead",
	}


# `history()` removed here when a save became a rail event — no callers left; archived in .archive/.
