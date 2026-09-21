# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Refuse removing a field that stored config still names — Frappe's `LinkExistsError`, asked of a field. A new consumer is one entry in `SOURCES`."""
import frappe
from frappe import _
from frappe.utils import get_link_to_form

from tatva_connect.taxonomy import grain as taxonomy_grain

LEAD, TASK = "CRM Lead", "CRM Task"


def guard_task_type(doc, deleting=False):
	"""A task type dropping schema fields, or being deleted, while a workflow or Smart View still names one."""
	from tatva_connect.activity.api import task_columns

	before = None if deleting else doc.get_doc_before_save()
	removed = _declared(doc) if deleting else (_declared(before) - _declared(doc) if before else set())
	removed -= set(task_columns())  # a native column still answers once the form stops declaring it
	own = _axes(doc)

	def lost_at(at):
		return removed - _task_fields_reaching(at, doc.name) if taxonomy_grain.overlaps(own, *at) else set()

	_refuse_if_used(TASK, removed, ("task_type", doc.name), removed, lost_at)


def guard_contract(doc):
	"""A contract dropping ticked fields while its bound sources, or its grain's consumers, still name one."""
	from tatva_connect.access import entitlement
	from tatva_connect.lead_sync.contract import allowed_field_keys

	before = doc.get_doc_before_save()
	removed = (_ticked(before) - _ticked(doc)) if before else set()
	own = _axes(doc)

	def lost_at(at):
		if not (doc.is_internal and taxonomy_grain.overlaps(own, *at)):
			return set()
		return {k for k in removed if not entitlement.field_in_any_grain_overlapping(k, at, excluding=tuple(own.values()))}

	_refuse_if_used(LEAD, removed, ("contract", doc.name), removed - allowed_field_keys(doc), lost_at)


def guard_catalog_field(doc):
	"""A catalog field being deleted is lost to every consumer, whatever its grain or contract."""
	_refuse_if_used(LEAD, {doc.name}, None, {doc.name}, lambda at: {doc.name})


def _refuse_if_used(doctype, removed, bound, bound_lost, lost_at):
	if not removed:
		return
	removal = frappe._dict(doctype=doctype, removed=removed, bound=bound, bound_lost=bound_lost, lost_at=lost_at)
	hits = [hit for source in SOURCES for hit in source(removal)]
	if not hits:
		return
	frappe.throw(
		"<br>".join(
			_("Cannot remove {0} because it is used by {1} {2} ({3})").format(
				frappe.bold(field), _(dt), get_link_to_form(dt, name, label or name), where
			)
			for field, dt, name, label, where in hits
		),
		frappe.LinkExistsError,
		title=_("Field In Use"),
	)


def _lost(removal, at=None, binding=None):
	"""What one consumer loses: a bound one only when the removal is on its record, or on every record."""
	if binding:
		return removal.bound_lost if removal.bound in (None, binding) else set()
	return removal.lost_at(tuple(at))


def _workflows(removal):
	"""Every draft and every published version, walked for the fields each node reads and writes."""
	from tatva_connect.workflow_engine import contract, registry, versions

	spelled = _workflow_spellings(removal)
	if not spelled:
		return []
	like = [["like", f"%{ref.rpartition('.')[2]}%"] for ref in spelled]
	drafts = set(frappe.get_all("CRM Workflow Node", or_filters=[["config_json", *c] for c in like], pluck="workflow"))
	graphs = [(_("draft"), versions.build_payload(frappe.get_doc("CRM Workflow", wf)), wf) for wf in drafts]
	graphs += [
		(_("version {0}").format(v.version_no), frappe.parse_json(v.payload_json), v.workflow)
		for v in frappe.get_all(
			"CRM Workflow Version", or_filters=[["payload_json", *c] for c in like], fields=["workflow", "version_no", "payload_json"]
		)
	]
	hits = []
	for state, payload, workflow in graphs:
		lost = _lost(removal, at=[payload.get(axis) for axis in taxonomy_grain.AXES])
		for node in payload.get("nodes") or []:
			config = registry.config_of(node)
			named = {r["ref"] for r in contract.reads_of(node["node_type"], config)} | contract.writes_of(node["node_type"], config)
			hits += [
				(key, "CRM Workflow", workflow, payload.get("workflow_name"), f"{state}, {node['node_id']}")
				for key in {spelled[ref] for ref in named if ref in spelled} & lost
			]
	return hits


def _smart_views(removal):
	from tatva_connect.smartview.catalog import activity_key
	from tatva_connect.smartview.query import _predicate_keys

	hits = []
	for view in frappe.get_all("CRM Smart View", fields=["name", "label", "activity_type", "predicate", "columns", *_grain_fields("CRM Smart View")]):
		named = set(frappe.parse_json(view.columns or "[]") or [])
		_predicate_keys(frappe.parse_json(view.predicate or "{}") or {}, named)
		if removal.doctype == TASK:
			lost = {activity_key(f) for f in _lost(removal, binding=("task_type", view.activity_type))}
		else:
			lost = _lost(removal, at=_axes(view, "CRM Smart View").values())
		hits += [(key, "CRM Smart View", view.name, view.label, _("column or filter")) for key in lost & named]
	return hits


def _facebook_forms(removal):
	if removal.doctype != LEAD:
		return []
	hits = []
	for source in frappe.get_all(
		"Lead Sync Source", filters={"facebook_lead_form": ("is", "set")}, fields=["facebook_lead_form", "api_mapping"]
	):
		lost = _lost(removal, binding=("contract", source.api_mapping))
		if lost:
			hits += [
				(q.mapped_to_crm_field, "Facebook Lead Form", source.facebook_lead_form, None, q.label)
				for q in frappe.get_all(
					"Facebook Lead Form Question",
					filters={"parent": source.facebook_lead_form, "mapped_to_crm_field": ("in", list(lost))},
					fields=["label", "mapped_to_crm_field"],
				)
			]
	return hits


def _intake_forms(removal):
	if removal.doctype != LEAD:
		return []
	hits = []
	for form in frappe.get_all("CRM Intake Form", fields=["name", "form_name", *_grain_fields("CRM Intake Form")]):
		lost = _lost(removal, at=_axes(form, "CRM Intake Form").values())
		hits += [
			(key, "CRM Intake Form", form.name, form.form_name, label)
			for key, label in _mapped_rows("CRM Intake Field Map", form.name, "label")
			if key in lost
		]
	return hits


def _lead_imports(removal):
	"""Only an import not yet run: a finished one's columns are history, not a consumer."""
	if removal.doctype != LEAD:
		return []
	hits = []
	for imp in frappe.get_all("CRM Lead Import", filters={"import_job": ("is", "not set")}, fields=["name", "contract"]):
		lost = _lost(removal, binding=("contract", imp.contract))
		hits += [
			(key, "CRM Lead Import", imp.name, None, column)
			for key, column in _mapped_rows("CRM Lead Import Column", imp.name, "source_column")
			if key in lost
		]
	return hits


SOURCES = (_workflows, _smart_views, _facebook_forms, _intake_forms, _lead_imports)


def _mapped_rows(child, parent, label_field):
	"""`(field_key, label)` for each row of a `target_table` + `target_field` mapping table."""
	return [
		(f"{r.target_table}:{r.target_field}", r.get(label_field))
		for r in frappe.get_all(child, filters={"parent": parent}, fields=["target_table", "target_field", label_field])
		if r.target_field
	]


def _workflow_spellings(removal):
	"""`{ref: key}` — every reference a workflow can spell a removed field by, as a read or as a write."""
	from tatva_connect.automation.fields import authored_name
	from tatva_connect.workflow_engine import refs

	if removal.doctype == TASK:
		return {refs.of_record(TASK, f): f for f in removal.removed}
	spelled = {}
	for row in frappe.get_all(
		"CRM Lead API Field", filters={"name": ("in", list(removal.removed))}, fields=["name", "section", "fieldname"]
	):
		sec = frappe.get_cached_doc("CRM Lead Section", row.section)
		spelled[refs.of_record(LEAD, authored_name(sec, row.fieldname))] = row.name
		spelled[refs.of_record(sec.target_doctype if sec.child_table_field else LEAD, row.fieldname)] = row.name
	return spelled


def _declared(doc):
	return {row.fieldname.strip() for row in doc.schema if (row.fieldname or "").strip()}


def _ticked(doc):
	return {row.field for row in doc.allowed_fields if row.field}


def _grain_fields(doctype):
	return [c for c in taxonomy_grain.columns(doctype) if c]


def _axes(row, doctype=None):
	"""`{axis: value}` off whichever columns the schema says carry the grain."""
	columns = taxonomy_grain.columns(doctype or row.doctype)
	return {axis: (row.get(c) or "") if c else "" for axis, c in zip(taxonomy_grain.AXES, columns, strict=True)}


def _task_fields_reaching(at, excluding):
	"""Fieldnames every OTHER task type overlapping `at` still declares."""
	types = [
		t.name
		for t in frappe.get_all("CRM Task Type", filters={"name": ("!=", excluding)}, fields=["name", *_grain_fields("CRM Task Type")])
		if taxonomy_grain.overlaps(_axes(t, "CRM Task Type"), *at)
	]
	if not types:
		return set()
	return set(frappe.get_all("CRM Task Type Field", filters={"parent": ("in", types), "parenttype": "CRM Task Type"}, pluck="fieldname"))
