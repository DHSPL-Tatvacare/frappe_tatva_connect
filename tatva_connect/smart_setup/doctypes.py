# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What Smart Setup must know about a doctype beyond its schema, asked of the brain that owns each answer.

VOCABULARY  the picklist rows a record's fields read by grain: no Link reaches them, so the walk cannot.
ADAPTERS    a doctype written through its own engine instead of a plain save. An adapter reads a record
            into the bundle, names its links, writes it on the target, and removes one a restore takes back.
"""
from typing import NamedTuple

import frappe

from tatva_connect.api.partner import _catalog
from tatva_connect.lead import multi_value
from tatva_connect.taxonomy import labels, picklist


class Adapter(NamedTuple):
	read: object  # (doctype, name) -> record
	links: object  # record -> {(doctype, name)}
	write: object  # (record, exists) -> None
	remove: object  # (doctype, name) -> None


def _picklist_rows(categories, grains):
	"""Each grain's picklist rows for each category, by name: the rows `picklist.offered_rows` gives a picker."""
	return [(labels.PICKLIST_VALUE, row.name) for category in categories for grain in grains
	        for row in picklist.offered_rows(category, grain)]


def _mapping_vocabulary(record):
	"""The picklist rows an API contract's granted fields read, for each program it writes leads into."""
	multi = set(multi_value.declared().values())
	section_doctype = _catalog()["section_doctype"]
	categories = set()
	for grant in record.get("allowed_fields") or []:
		field = frappe.db.get_value("CRM Lead API Field", grant["field"], ["section", "fieldname"], as_dict=True)
		if not field:
			continue
		df = (multi_value.value_field() if grant["field"] in multi
		      else frappe.get_meta(section_doctype[field.section]).get_field(field.fieldname))
		if df and df.fieldtype == "Link" and df.options == labels.PICKLIST_VALUE:
			categories.add(picklist.category_of(field.fieldname))
	programs = ([record["program"]] if record.get("program")
	            else [row["program"] for row in record.get("allowed_programs") or []] or [""])
	return _picklist_rows(categories, [(record["vertical"], record["crm_group"], program) for program in programs])


def _task_type_vocabulary(record):
	"""The picklist rows a task type's Link fields read, at its own grain."""
	categories = {picklist.category_of(f["fieldname"]) for f in record.get("schema") or []
	              if f.get("fieldtype") == "Link" and f.get("options") == labels.PICKLIST_VALUE}
	return _picklist_rows(categories, [(record["vertical"], record["group"], record.get("program") or "")])


VOCABULARY = {"CRM Lead API Mapping": _mapping_vocabulary, "CRM Task Type": _task_type_vocabulary}


def _workflow_read(doctype, name):
	"""A workflow as its latest graph and layout, read by the canvas's own reader; never its versions or runs."""
	from tatva_connect.workflows import api as workflows

	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")  # as the canvas's own Duplicate reads a workflow
	return {"doctype": doctype, "name": name, "workflow_name": doc.workflow_name, "canvas_json": doc.canvas_json,
	        "entry_node": doc.entry_node,
	        "nodes": [{k: v for k, v in node.items() if k != "name"} for node in workflows._nodes_of(name)]}


def _workflow_links(record):
	"""The records a workflow's nodes name: every setting the node registry declares as a Link, and every
	predicate value compared against a Link field, read the way the publish gate reads them."""
	from tatva_connect.workflow_engine import contract, refs, registry

	nodes = [(node.get("node_type"), registry.config_of(node)) for node in record["nodes"]]
	subject = next((config.get("subject_doctype") for kind, config in nodes if kind == "Trigger"), None)
	index = refs.readable_index(subject or "", "CRM Lead")
	found = set()
	for kind, config in nodes:
		for field in (registry.NODE_TYPES.get(kind) or {}).get("config") or []:
			value = config.get(field["name"])
			if field.get("link") and isinstance(value, str) and value and not refs.is_reference(value):
				found.add((field["link"], value))
			if field.get("type") != "Predicate":
				continue
			for rule in contract._predicate_rules(value):
				described = index.get(rule.get("field")) or {}
				master = (described.get("pick") or {}).get("target") or described.get("options")
				if described.get("type") == "Link" and master:
					found |= {(master, one) for one in registry._value_items(rule.get("value"), rule.get("operator"))
					          if frappe.db.exists(master, one)}
	return found


def _workflow_write(record, exists):
	"""Create the workflow as a Draft if it is new, then save its graph through the canvas's own `save_draft`,
	which refuses a workflow that is not a Draft on this site: an import never disarms a live journey."""
	from tatva_connect.workflows import api as workflows

	if not exists:
		workflows.create_workflow(record["workflow_name"])
	workflows.save_draft(record["name"], record["nodes"], record["canvas_json"], record["entry_node"])


def _workflow_remove(doctype, name):
	"""Empty the workflow through the canvas's own `save_draft` (a Draft only), then delete the row nothing links to."""
	from tatva_connect.workflows import api as workflows

	workflows.save_draft(name, [], None, None)
	frappe.delete_doc(doctype, name)


ADAPTERS = {"CRM Workflow": Adapter(_workflow_read, _workflow_links, _workflow_write, _workflow_remove)}
