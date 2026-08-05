# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Resolve a Link value to the label a human reads: the target doctype's `title_field`.

Six doctypes are named by a `format:` autoname built from the grain, so their primary key is data:
`CRM Task Type` is `{vertical}::{group}::{program}::{type_name}`. Printing that key gives a rep
`Goodflip-Care::Anaya::::Identify PSP Category`. The clean name is the doctype's own `title_field`,
read from meta, never a hardcoded doctype/field pair.

Where a payload carries the key AND the label, the contract is:

    <key>        the primary key. The client filters, saves and looks up config on it. It stays.
    <key>_label  the title_field. Display, search, sort.

Never replace the key with the label in a ROW payload: a row is what the client groups by, and two
stages that share a name across programs are distinct keys but one label, so the group-by would merge
them. A FILTER is the other way round — the question is cross-grain, the picker offers the label
(`label_query`) and `filter_on` reads it back as every key that carries it, which is why a rep asking
for "Not Interested" no longer silently asks for one programme's.

The SPA list does not use this module at all; it uses the framework's own `_link_titles` map (see
api/list_link_titles.py), which ships the label alongside the untouched key. This module is for the
hand-built payloads that have no such map.

`api.list_link_titles._resolve_title` calls `title_of` for the lookup, so there is one implementation
of "read the title_field"; it adds the framework's `show_title_field_in_link` and read-permission
gates on top, which are the list map's semantics, not this module's.

Permissions: `frappe.get_cached_value` does not check them, matching the `frappe.db.get_value` calls
this replaced. Callers have already gated the record the value was read off.
"""
import frappe
from frappe.utils import cint

# The grain masters whose primary key is a composite. Named here so no call site spells them itself.
TASK_TYPE = "CRM Task Type"
LEAD_STAGE = "CRM Lead Stage"
PICKLIST_VALUE = "CRM Picklist Value"

# The three a rep is offered a PICKER on — earned by being offered, never by the shape of a key (nothing here parses one); the guard test goes red the day a fourth reaches a picker.
COMPOSITE = (LEAD_STAGE, TASK_TYPE, PICKLIST_VALUE)

# The dotted path a Link control hands `frappe.desk.search.search_link` as its `query`. Spelled once.
LABEL_QUERY = "tatva_connect.taxonomy.labels.label_query"

# The most options one Link picker is given, whichever query answers it (taxonomy.picklist reads it too).
OPTION_CAP = 50

# The operators that ask "is this value one of a set", and what each becomes once a label is read as the several keys it means. A LIKE, an is-set and a range are different questions and are left alone.
_MEMBERSHIP = {
	"=": "in",
	"==": "in",
	"equals": "in",
	"in": "in",
	"!=": "not in",
	"not equals": "not in",
	"not in": "not in",
}


def is_composite(doctype):
	"""Whether this master's primary key is a composite, so what a user means by a value is its LABEL."""
	return bool(doctype) and doctype in COMPOSITE


def link_query(doctype):
	"""The scoped query a FILTER control must use for a Link at `doctype`, or None for the framework's own
	search. THE ONE decision — every menu that describes a Link field asks it here, so the client is handed
	a query name and never a list of doctypes to reason about."""
	return LABEL_QUERY if is_composite(doctype) else None


def title_field(doctype):
	"""The column holding this doctype's human label, or None where it declares none. ONE reader, so the
	three directions below cannot disagree about where a label lives."""
	if not doctype:
		return None
	try:
		field = frappe.get_meta(doctype).title_field
	except Exception:
		# On the PRIMARY, always: a reader of this module may be running on the replica and an Error Log is an INSERT.
		frappe.write_only()(frappe.log_error)(f"labels: cannot read meta of {doctype}", frappe.get_traceback())
		return None
	return field if field and field != "name" else None


def title_of(doctype, value):
	"""The target's title_field for one value, or None when there is no title to read. Never raises:
	a bad doctype or a title_field naming a missing column degrades to no-label, never a 500."""
	if not (doctype and value):
		return None
	field = title_field(doctype)
	if not field:
		return None
	try:
		return frappe.get_cached_value(doctype, value, field) or None
	except Exception:
		frappe.write_only()(frappe.log_error)(f"labels: cannot read title_field of {doctype}", frappe.get_traceback())
		return None


def labels_of(doctype, txt=None):
	"""The DISTINCT labels this master offers — what a user picks from, where the key is the database's.

	A grain master keys one human value once per grain, so `CRM Lead Stage` holds four rows named
	`{programme}::Not Interested`. Offering the rows offers the same choice four times and makes the
	reader pick a programme they were not asked about. Offering the labels asks the question once.

	Read from the master, never declared, so a value added today is offered on the next read with nothing
	to regenerate. Empty where the target declares no title_field, which leaves an ordinary doctype alone."""
	field = title_field(doctype)
	if not field:
		return []
	filters = {field: ["like", f"%{txt}%"]} if txt else None
	return [row for row in frappe.get_all(doctype, filters=filters, pluck=field, distinct=True,
	                                      order_by=field) if row]


def keys_of(doctype, value):
	"""Every key `value` means: itself when it already is one, else every key carrying that label.

	The inverse of `title_of`, reading the same `title_field`, so the two cannot drift apart. `exists`
	decides which case applies rather than the shape of the string — splitting on `::` would assume an
	autoname format the master owns, and an ordinary doctype falls through to an exact match untouched."""
	if not (doctype and value):
		return []
	if frappe.db.exists(doctype, value):
		return [value]
	field = title_field(doctype)
	if not field:
		return [value]
	return frappe.get_all(doctype, filters={field: value}, pluck="name") or [value]


@frappe.whitelist()
def label_query(doctype, txt, searchfield, start, page_len, filters):
	"""The DISTINCT labels a FILTER control offers — `search_link`'s custom-query seam, the same signature
	and the same `[(value, label), ...]` return shape as `taxonomy.picklist.picklist_query`.

	Value AND label are the label itself, because a filter matches on what the reader picked: `filter_on`
	expands it back to every key on the way into a query. A WRITE picker is a different question — it has a
	record in hand, so that record's grain already leaves one key per label — and it keeps the query it had.

	Capped like its twin, and read off the master on every call, so a stage added a second ago is offered
	with nothing to regenerate. A doctype that is not a composite master is answered by nobody here."""
	if not is_composite(doctype):
		return []
	offered = labels_of(doctype, txt)
	return [(row, row) for row in offered[: min(cint(page_len) or 20, OPTION_CAP)]]


def filter_on(doctype, operator, value):
	"""The (operator, value) a filter must REALLY run as, once a LABEL is read as the identity it is.

	A filter picker offers a composite master's labels, so what a rep asks for is `Not Interested` while
	every column holds `Sigrima::Not Interested`. One label means several keys, and several keys are a set,
	so an equality becomes an `in` and a negation a `not in`. Nothing else moves: a value that is already a
	key, an operator that is not a membership test, and any doctype that is not a composite master all come
	back exactly as they arrived — which is what keeps a saved view holding a raw key answering as it did.

	The pair is the shape BOTH engines already speak — `list_engine` puts it in a filters dict, `smartview`
	hands it to `_criterion` — so this rule is written once and called twice, never implemented twice."""
	wanted = _MEMBERSHIP.get(str(operator or "").strip().lower())
	if not (wanted and is_composite(doctype)):
		return operator, value
	given = list(value) if isinstance(value, list | tuple) else [value]
	keys = list(dict.fromkeys(key for v in given for key in keys_of(doctype, v)))
	if keys == given:
		return operator, value
	return wanted, keys


def label(value, doctype):
	"""The label for one value, falling back to the raw value so a field never blanks."""
	if not value:
		return ""
	return title_of(doctype, value) or value


def labels(values, doctype):
	"""{pk: label} for a row projection. Deduped, so a 100-row page of 5 distinct stages costs 5 cached
	reads, not 100. Build this once above the loop rather than calling `label()` inside it."""
	wanted = {v for v in (values or []) if v}
	return {v: label(v, doctype) for v in wanted}


def shown(doctype, fieldname, value):
	"""A field's value as a human should read it: a Link to a grain master holds a composite key, so
	show the target's title. Any other fieldtype passes through untouched. For the callers that hold a
	doctype and a fieldname rather than a Link target: notifications, WhatsApp params, rule previews."""
	if not (doctype and fieldname and isinstance(value, str) and value):
		return value
	try:
		df = frappe.get_meta(doctype).get_field(fieldname)
	except Exception:
		return value
	if not df or df.fieldtype != "Link" or not df.options:
		return value
	return label(value, df.options)


def stage_of(row):
	"""THE ONE reading of "what stage is this lead at" — label plus the stage's own colour.

	Every surface that shows a lead's stage resolves through here: the hover-preview card
	(`api/lead_preview`) and the spotlight index (`search/index`). They answered differently before this
	existed — one read `custom_substage or custom_stage` and looked the master up, the other read
	`custom_stage` alone, split the composite key on `::` and fell back to the LEAD STATUS — so a lead
	with no stage read as "Nurture" in search and as nothing on the card.

	Sub-stage wins: it is the leaf a rep actually picks, and `custom_stage` is the parent derived from it.
	The label is the master's `title_field`, never a split of the primary key, and the colour is the
	master's own `color` — operator data, blank until someone sets it, and never defaulted to a hue here.

	NO fallback to `status`. A lead status and a lead stage are different questions, and answering one
	with the other is what made the two surfaces disagree.

	Takes anything with the two fields — a Document or a plain row. Returns `(label, color)`, both "".
	"""
	key = (row.get("custom_substage") if hasattr(row, "get") else None) or (
		row.get("custom_stage") if hasattr(row, "get") else None
	)
	if not key:
		return "", ""
	stage = frappe.get_cached_value(LEAD_STAGE, key, ["display_label", "stage", "color"], as_dict=True)
	if not stage:
		return "", ""
	return (stage.display_label or stage.stage or ""), (stage.color or "")
