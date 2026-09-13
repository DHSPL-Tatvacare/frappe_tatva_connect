"""The ONE trigger-context toolbox — subject resolution + the field-diff + the context/field-type
builders, shared by everything that reacts to a doc write.

These brains are not engine-specific — they resolve the parent lead, diff the watched fields, and assemble
the context a criteria predicate reads. The Flow front-door (`workflow_engine.triggers`) and the location
engine both build on them, so they live in one neutral module with one
implementation (A.8, no second copy).
"""
import frappe
from frappe.utils import cstr

from tatva_connect.automation import subjects


def subject(doc):
	"""The subject a Flow/guard acts ON — the parent Lead. Name resolution is delegated to the ONE brain
	(`subjects.resolve_lead_name`): Lead → itself, Task → its parent Lead. Returns the Lead DOC so callers
	read `.name` + fields; `None` for an unresolvable subject (fail-closed). A Lead trigger returns the
	already-fetched doc (no reload)."""
	lead_name = subjects.resolve_lead_name(doc)
	if not lead_name:
		return None
	return doc if doc.doctype == "CRM Lead" else frappe.get_doc("CRM Lead", lead_name)


def subject_axes(subject_doc):
	"""(vertical, group, program) read straight off the in-memory subject Lead DOCUMENT — never a DB round
	trip. `subject()` always returns the real CRM Lead doc (itself, or the loaded parent Lead for a Task),
	already grain-stamped by `before_validate`, so its own fields are the answer."""
	return (
		subject_doc.get("custom_vertical") or "",
		subject_doc.get("custom_group") or "",
		subject_doc.get("custom_current_program") or "",
	)


def diff_watched_fields(doc):
	"""Return {fieldname: (old, new)} for every watched field whose value changed on this save. Casts both
	sides through the field's own fieldtype before comparing. The Watchable registry is read per-doctype
	and cached for the request.

	AN INSERT DIFFS AGAINST AN EMPTY BEFORE — a task born `Done` has arrived at `Done`, which is what
	`changed to` means. frappe runs `on_update` inside `insert()` (`document.py:493`), so the dispatcher
	already reached these workflows; with no before-image every `changed to` in the product went deaf to an
	activity logged ad hoc, because `save_activity` inserts the task already carrying its status.
	`flags.in_insert` (set at `document.py:486`) is what tells an insert from a re-save that merely lost its
	before-image; the latter still returns {}, unchanged."""
	watched = watchable_fields_for(doc.doctype)
	if not watched:
		return {}
	before = doc.get_doc_before_save()
	sections = [f for f in watched if "." in f] if doc.doctype == "CRM Lead" else []
	if not before:
		if not doc.flags.get("in_insert"):
			return {}  # no before-state and not an insert (e.g. a migration re-save) - nothing to diff
		before = frappe._dict()  # every watched field moved from nothing; the loop below decides which
		sections = []  # a section row born WITH the lead is its first arrival, never a change to one
	out = _section_diff(doc, before, sections) if sections else {}
	for fieldname in watched:
		if fieldname in sections:
			continue  # diffed above: a section column is read through its section, not off the lead
		df = doc.meta.get_field(fieldname)
		if df is None:
			continue  # a stale registry row pointing at a removed field - skip, don't crash
		old, new = before.get(fieldname), doc.get(fieldname)
		try:
			if frappe.utils.cast(df.fieldtype, old) != frappe.utils.cast(df.fieldtype, new):
				out[fieldname] = (old, new)
		except Exception:  # nosec B110 - an uncastable value falls back to raw equality
			if old != new:
				out[fieldname] = (old, new)
	return out


def _section_diff(doc, before, paths):
	"""Watched child-section columns, diffed on the CURRENT reading of each side.

	The reading, never the rows: `section_values` is what a criterion reads and what the Data tab and a Smart
	View show, so `changed` here means what the author means by it. A multi-row section reads its newest
	value, so a fresh row carrying a later value IS a change — which is how one more acquisition touch is a
	patient arriving again rather than a record being edited.

	ONLY the sections a watched column names are read, so a site that watches one column pays for one section
	and a site that watches none pays nothing at all (`diff_watched_fields` never calls here).
	"""
	from tatva_connect.lead import multirow
	from tatva_connect.partner_api.doctype.crm_lead_section import crm_lead_section

	by_table = {}
	for path in paths:
		by_table.setdefault(path.split(".", 1)[0], []).append(path)
	out = {}
	for table, table_paths in by_table.items():
		section = crm_lead_section.section_for_child(table)
		if section is None:
			continue  # a watched column whose section is gone - skip, don't crash
		now_row = multirow.current_for_section(doc, section) or {}
		was_row = multirow.current_for_section(before, section) or {}
		for path in table_paths:
			column = path.split(".", 1)[1]
			old, new = was_row.get(column), now_row.get(column)
			if cstr(old) != cstr(new):
				out[path] = (old, new)
	return out


def context_for(doc, changed, lead=None):
	"""The trigger context a criteria predicate reads — a `refs.Values`, NAMESPACED by the doc's own slug.

	ONE VOCABULARY, IN BOTH PLACES A PREDICATE IS JUDGED. A predicate control is one control, and an author
	who builds `Status is New` means one thing by it. The engine judges it in two entirely different places:
	a TRIGGER predicate here, at dispatch, against the doc that fired; a BRANCH predicate at execution,
	against the journey's state. Namespace one and not the other and the same control means two different
	things — the Trigger/Route divergence this codebase has already found once. Both build a `Values`, so
	there is one grammar and one resolver.

	What lands, all under `frappe.scrub(doc.doctype)`:

	  * the doc's own persistable fields (`get_valid_dict`);
	  * for each changed watched field, `<slug>.<field>__before` carrying the old value — the pair the
	    `changed to` / `changed from…to` operators read. The suffix goes on the FIELD, never on the source;
	    `refs` owns that grammar and the reasoning is in its docstring;
	  * CRM Task's activity-schema values, keyed by their own LOGICAL schema fieldname, `setdefault`-merged
	    so a genuine doc column always wins a name clash.

	Written into a BUCKET rather than handed over as a record loader, deliberately: this doc is mid-save
	and uncommitted, so the in-memory values are the truth and a re-read would see the old row. The durable
	run does the opposite for the same reason — see `interpreter._refreshed_state`.
	"""
	from tatva_connect.workflow_engine import refs

	values = dict(doc.get_valid_dict())
	for fieldname, value in activity_values(doc).items():
		values.setdefault(fieldname, value)
	for fieldname, value in section_values(doc).items():
		values.setdefault(fieldname, value)
	for fieldname, (old, _new) in changed.items():
		values[f"{fieldname}{refs.BEFORE}"] = old
	built = refs.Values(buckets={refs.slug(doc.doctype): values})
	# Offered LAZILY and a no-op when the doc IS the lead, so a trigger naming only its own record pays nothing.
	if lead is not None:
		built.offer_record(refs.slug("CRM Lead"), lambda: bucket_of(lead))
	return built


def bucket_of(doc):
	"""One record's values in the shape a `Values` reads them from — what every record loader hands back."""
	from tatva_connect.workflow_engine import refs

	return context_for(doc, {}).buckets.get(refs.slug(doc.doctype), {})


def activity_values(doc):
	"""CRM Task's activity-schema submitted values, keyed by their SCHEMA fieldname — NOT the promoted
	column / payload key `activity.api.compute_activity` routed them to. A criterion is authored against
	the schema fieldname (outcome/training_status/…), so the context must expose the SAME name at fire
	time. Reuses the ONE existing brain (`activity.api._task_values` + `_type_config`). Empty for a
	non-CRM-Task subject or a plain task with no activity type.

	Reading is scoped by the workflow's GRAIN at author time (`describe._criterion_fields`), which is the
	same contract execution enforces — there is no second per-field read flag to consult here."""
	if doc.doctype != "CRM Task" or not doc.get("custom_task_type"):
		return {}
	from tatva_connect.activity.api import _task_values, _type_config

	cfg = _type_config(doc.custom_task_type)
	if not cfg:
		return {}
	return _task_values(doc, cfg)


def section_values(doc):
	"""CRM Lead's child-section values flattened to one row each, keyed `<child_table>.<column>` — the
	same dotted path `describe.field_catalog` offers an author and `refs.parse` reads as one field.

	A lead's counters and profile values live in child rows, so a Route thresholding on one of them has
	nothing to read without this. The meta carries the Table field and never its columns, which is why the
	vocabulary needs the mirror union in `describe.fields_for_doctype`.

	Every section resolves through `multirow.current_for_section` — a singleton gives its one row, a multi-row
	section its current reading (per column, the newest row that has a value). That is what the Data tab and a
	Smart View already show, so a column means the same thing wherever it is read; a rule that could see
	nothing would break that as surely as one that read a different value."""
	if doc.doctype != "CRM Lead":
		return {}
	from tatva_connect.lead import multirow
	from tatva_connect.partner_api.doctype.crm_lead_section import crm_lead_section

	out = {}
	for section in crm_lead_section.child_sections():
		row = multirow.current_for_section(doc, section)
		if row is None:
			continue
		for fieldname, value in row.items():
			out[f"{section.child_table_field}.{fieldname}"] = value
	return out


def fields_for(*doctypes):
	"""{reference: declaration} for the records a predicate may name — the WHOLE answer, not a slice of it.

	`rules._rule_match` reads two things from it: the `type` a comparison casts through, and the `pick` a
	Link is chosen from. It used to hand back `{ref: type}`, which threw away WHICH master a Link points
	at — the one fact a composite `::` key cannot be read without, so an author's `Patient no more` could
	never be matched against a column holding `Sigrima::Patient no more`.

	Takes MORE than one doctype because a Route reads the triggering record AND the lead behind it, and
	handing it only one made a rule on the other raise. `refs.readable_index` is the one walk and the one
	cache, and it already keys by the NAMESPACED `ref` (`crm_lead.mobile_no`) that `context_for` uses —
	so this holds no vocabulary of its own and no longer narrows the answer either."""
	from tatva_connect.workflow_engine import refs

	return refs.readable_index(*doctypes)


def watchable_fields_for(doctype):
	"""The enabled can_watch fieldnames for a doctype, cached per-request (the query brain is
	`fields.watchable_fields` — this is just the cache)."""
	from tatva_connect.automation import fields

	cache = frappe.flags.setdefault("_watchable_fields_cache", {})
	if doctype not in cache:
		cache[doctype] = fields.watchable_fields(doctype)
	return cache[doctype]
