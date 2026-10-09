# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Smart Setup Copy: one grain's setup put onto another grain on this site, through the Grain recipe's bundle."""
import frappe
from frappe.model.naming import set_new_name

from tatva_connect.automation import rules
from tatva_connect.smart_setup import bundle, parts, recipes
from tatva_connect.smartview import catalog
from tatva_connect.taxonomy import grain
from tatva_connect.workflow_engine import refs, registry

RECIPE = recipes.WHOLE_GRAIN
LEAD = frappe.scrub("CRM Lead")


def build(source, target):
	"""The copy bundle's JSON text: the moved records, and for each its source name and the state it lands in."""
	return bundle.build(RECIPE, [source], remap=_Mover(source, target).remap)


class _Mover:
	"""Moves records from the source grain to the target: grain parts first, then names, then links to moved names."""

	def __init__(self, source, target):
		src, dst = (frappe.get_cached_doc("CRM Grain", name) for name in (source, target))
		self.parts = {axis: (src.get(axis), dst.get(axis)) for axis in grain.AXES
		              if src.get(axis) and src.get(axis) != dst.get(axis)}
		self.label = dst.get("program") or dst.get("group")
		self.moved = {}

	def remap(self, records):
		"""The bundle parts: the records the grain parts move, renamed and repointed, and what each was and lands as."""
		out = [(record, record["name"]) for record in records
		       if record["doctype"] != "CRM Grain" and self._walk(record, self._part)]
		arrives = {}
		for record, old in out:
			arrives[(record["doctype"], old)] = parts.lands(record)
			self.moved[(record["doctype"], old)] = self._new_name(record)
		names = {old: new for (_doctype, old), new in self.moved.items()}
		for record, old in out:
			self._walk(record, lambda doctype, value, axis=None: self.moved.get((doctype, value), value) if doctype
			           else names.get(value, value))
			record["name"] = self.moved[(record["doctype"], old)]
		return {"records": [record for record, _old in out],
		        "copied": [{"doctype": record["doctype"], "name": record["name"], "from": old,
		                    "arrives_as": arrives[(record["doctype"], old)]}
		                   for record, old in out]}

	def _part(self, doctype, value, axis=None):
		"""The target's value for a cell naming a source grain part, else the value unchanged."""
		axis = grain.axis_of(doctype) if doctype else axis
		old, new = self.parts.get(axis, (None, None))
		return new if old and value == old else value

	def _walk(self, record, swap):
		"""Apply `swap(doctype, value, axis)` to every cell naming a record or a grain value; True if any changed."""
		changed = False
		for row, key, doctype, axis, listed in _cells(record):
			value = row.get(key)
			if listed:
				items = rules._split_list(value)
				swapped = [swap(doctype, item, axis) for item in items]
				new = value if swapped == items else (swapped if isinstance(value, list) else ", ".join(swapped))
			else:
				new = swap(doctype, value, axis)
			if new != value:
				row[key], changed = new, True
		_CLOSE.get(record["doctype"], lambda r: None)(record)
		return changed

	def _new_name(self, record):
		"""The name Frappe's own naming gives the moved record; a typed name gets the target swapped in or appended."""
		autoname = frappe.get_meta(record["doctype"]).autoname or ""
		if autoname == "hash":
			return f"{record['name']}-{frappe.scrub(self.label).replace('_', '-')}"
		if autoname.startswith("field:"):
			field = autoname.removeprefix("field:")
			record[field] = self._retitled(record[field])
		elif autoname.lower() == "prompt":
			record["name"] = self._retitled(record["name"])
		doc = frappe.get_doc(dict(record))
		set_new_name(doc)
		return doc.name

	def _retitled(self, text):
		swapped = text
		for old, new in self.parts.values():
			swapped = swapped.replace(old, new)
		return swapped if swapped != text else f"{text} ({self.label})"


# -- where a record names another record or a grain value ---------------------

def _cells(record):
	"""(row, key, doctype, axis, listed) for every Link cell, plus each cell a record keeps in its own JSON."""
	meta = frappe.get_meta(record["doctype"])
	rows = [(meta, record)] + [(frappe.get_meta(df.options), row) for df in meta.get_table_fields()
	                           for row in record.get(df.fieldname) or []]
	cells = [(row, df.fieldname, df.options, None, False) for row_meta, row in rows for df in row_meta.get_link_fields()]
	return cells + _JSON_CELLS.get(record["doctype"], lambda r: [])(record)


def _workflow_cells(record):
	"""A workflow's cells: every value in each node's config, the Trigger's grain read as grain."""
	cells = []
	for node in record["nodes"]:
		node["_config"] = registry.config_of(node)
		cells += _json_cells(node["_config"], trigger=node.get("node_type") == "Trigger")
	return cells


def _view_cells(record):
	"""A standard view's cells: every value in its conditions."""
	record["_predicate"] = frappe.parse_json(record.get("predicate") or "{}") or {}
	return _json_cells(record["_predicate"])


def _json_cells(tree, trigger=False):
	"""(row, key, None, axis, listed) for every text value in a JSON tree; a condition or update on a lead grain field is grain."""
	cells, pending = [], [tree]
	while pending:
		row = pending.pop()
		items = row.items() if isinstance(row, dict) else enumerate(row) if isinstance(row, list) else ()
		for key, value in items:
			if isinstance(value, dict) or (isinstance(value, list) and not all(isinstance(v, str) for v in value)):
				pending.append(value)
			elif isinstance(value, str | list) and isinstance(row, dict):
				axis = key if trigger and key in grain.AXES else _value_axis(row) if key == "value" else None
				listed = isinstance(value, list) or (key == "value" and row.get("operator") in rules._MEMBERSHIP_OPS)
				cells.append((row, key, None, axis, listed))
	return cells


def _value_axis(row):
	"""The grain axis a condition's field or an update's field carries on the lead, or None."""
	if "field" in row:
		parsed = refs.parse(row["field"])
		field = parsed[1] if parsed and parsed[0] == LEAD else (catalog._lead_catalog().get(row["field"]) or {}).get("fieldname")
	else:
		field = row.get("name")
	return dict(zip(grain.columns("CRM Lead"), grain.AXES, strict=True)).get(field)


def _close_workflow(record):
	for node in record["nodes"]:
		node["config_json"] = frappe.as_json(node.pop("_config"))


def _close_view(record):
	record["predicate"] = frappe.as_json(record.pop("_predicate"))


_JSON_CELLS = {"CRM Workflow": _workflow_cells, "CRM Smart View": _view_cells}
_CLOSE = {"CRM Workflow": _close_workflow, "CRM Smart View": _close_view}
