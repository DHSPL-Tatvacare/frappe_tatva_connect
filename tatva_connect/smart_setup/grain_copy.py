# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Smart Setup Copy: one grain's setup put onto another grain on this site, through the Grain recipe's bundle.

A copy moves the setup and nothing that belongs to one grain only: no person (any Link to User), no partner's
contract (`parts.Left`), and never a record onto a name another grain holds. Each record is swapped onto the target's
grain, left without its people, then named by the first of these that is free or already the target's: its swapped
fields and title, then with the setup's suffix, then with the target grain's own code. The code is fixed per grain,
so a second copy onto the same grain lands on the same names and changes nothing.
"""
import re

import frappe
from frappe import _
from frappe.model.naming import set_new_name
from frappe.utils.data import sha256_hash

from tatva_connect.automation import rules
from tatva_connect.smart_setup import bundle, parts, recipes
from tatva_connect.smartview import catalog
from tatva_connect.taxonomy import grain
from tatva_connect.workflow_engine import refs, registry

RECIPE = recipes.WHOLE_GRAIN
LEAD = frappe.scrub("CRM Lead")
PERSON = "User"
ABBREVIATION = "abbreviation"
CODE_LENGTH = 3


def build(source, target, suffix=None):
	"""The copy bundle's JSON text: the moved records, and for each its source name and what it left behind."""
	members = parts.copied(recipes.get(RECIPE)["members"])
	return bundle.build(RECIPE, [source], remap=_Mover(source, target, suffix).remap, members=members)


def suffix_for(grain_row):
	"""The grain's abbreviations run together (GF + FS = GFFS), from every axis whose master declares one; blank if one is unset."""
	codes = [_abbreviation(axis, value) for axis, value in _abbreviated(grain_row)]
	return "".join(codes) if codes and all(codes) else ""


def _abbreviated(grain_row):
	"""(axis, value) for each axis the grain sets whose master carries an abbreviation."""
	row = frappe.get_cached_doc("CRM Grain", grain_row)
	return [(axis, row.get(axis)) for axis in grain.AXES
	        if row.get(axis) and frappe.get_meta(grain.master(axis)).has_field(ABBREVIATION)]


def _abbreviation(axis, value):
	return frappe.db.get_value(grain.master(axis), value, ABBREVIATION) or ""


class _Mover:
	"""Moves records from the source grain to the target: grain parts first, then people out, then names, then links to moved names."""

	def __init__(self, source, target, suffix=None):
		src, dst = (frappe.get_cached_doc("CRM Grain", name) for name in (source, target))
		self.parts = {axis: (src.get(axis), dst.get(axis)) for axis in grain.AXES
		              if src.get(axis) and src.get(axis) != dst.get(axis)}
		self.codes = _code_pairs(source, target)
		self.suffix = (suffix or "").strip()
		self.axes = {axis: dst.get(axis) or "" for axis in grain.AXES}
		self.code = sha256_hash(target)[:CODE_LENGTH]  # the same grain always gets the same code
		self.moved, self.taken = {}, set()

	def remap(self, records):
		"""The bundle parts: the records the grain parts move, renamed and repointed, what each was and what it left behind."""
		out = [(record, record["name"]) for record in records
		       if record["doctype"] != "CRM Grain" and self._walk(record, self._part)]
		notes = {}
		for record, old in out:
			notes[(record["doctype"], old)] = _note(_leave_people(record), parts.is_on(record["doctype"], old) is False)
			self.moved[(record["doctype"], old)] = self._new_name(record)
		names = {old: new for (_doctype, old), new in self.moved.items()}
		for record, old in out:
			self._walk(record, lambda doctype, value, axis=None: self.moved.get((doctype, value), value) if doctype
			           else names.get(value, value))
			record["name"] = self.moved[(record["doctype"], old)]
		return {"records": [record for record, _old in out],
		        "copied": [{"doctype": record["doctype"], "name": record["name"], "from": old,
		                    "note": notes[(record["doctype"], old)]} for record, old in out]}

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
		"""The first name Frappe's own naming gives the moved record that no other grain holds; none marks it refused."""
		meta = frappe.get_meta(record["doctype"])
		autoname, source = meta.autoname or "", record["name"]
		title = (autoname.removeprefix("field:") if autoname.startswith("field:") else "name" if autoname.lower() == "prompt"
		         else None if autoname.startswith("format:") else meta.title_field)  # a format: name follows the swapped fields
		text = self._retitled(record[title]) if title and record.get(title) else None
		for tag in self._tags():
			if autoname == "hash":
				name = f"{source}-{frappe.scrub(tag).replace('_', '-')}" if tag else source
			else:
				if text:
					record[title] = f"{text} ({tag})" if tag else text
				doc = frappe.get_doc(dict(record))
				set_new_name(doc)
				name = doc.name
			if self._free(record, name):
				self.taken.add((record["doctype"], name))
				return name
		record[bundle.REFUSAL] = _("{0}: {1} would land on a name another grain holds. Rename the other one, or type another Name Suffix.").format(
			parts.label(record["doctype"]), name)
		return name

	def _retitled(self, text):
		"""The title with the target's grain values and whole-word abbreviations in."""
		for old, new in self.parts.values():
			text = text.replace(old, new)
		for old, new in self.codes:
			text = re.sub(rf"\b{re.escape(old)}\b", lambda _m, new=new: new, text)
		return text

	def _tags(self):
		"""What a title may carry, most readable first: nothing, the suffix, then the suffix with the grain's code."""
		return [None] + ([self.suffix] if self.suffix else []) + [f"{self.suffix}-{self.code}" if self.suffix else self.code]

	def _free(self, record, name):
		"""No other record of this copy takes the name, and on this site it is unused or holds the moved record's own grain."""
		if (record["doctype"], name) in self.taken:
			return False
		columns = {col: axis for axis, col in zip(grain.AXES, grain.own_columns(record["doctype"]), strict=True) if col}
		held = frappe.db.get_value(record["doctype"], name, list(columns) or "name", as_dict=True)
		# A record's own axes may be broader than the target's (a group-level option); a workflow's bundle carries none, so the target's stand in.
		own = {col: (record.get(col) or "") if col in record else self.axes[axis] for col, axis in columns.items()}
		return not held or all((held.get(col) or "") == own[col] for col in columns)


def _code_pairs(source, target):
	"""(source, target) abbreviations to swap in titles, the run-together codes first, then each axis's own."""
	src, dst = dict(_abbreviated(source)), dict(_abbreviated(target))
	pairs = [(suffix_for(source), suffix_for(target))]
	pairs += [(_abbreviation(axis, src[axis]), _abbreviation(axis, dst[axis])) for axis in src if axis in dst]
	return [(old, new) for old, new in pairs if old and new and old != new]


def _note(people, off_at_source):
	"""What a copied record left behind, as its Reason reads it."""
	said = [_("People not carried: {0}. Add this grain's own before turning it on.").format(len(people))] if people else []
	return " ".join(said + ([_("Switched Off on the source grain. Check it before you turn it on.")] if off_at_source else []))


# -- who a record names: a person never moves between grains -------------------

def _leave_people(record):
	"""Blank every Link to a person and drop each row that exists to name one; the people left behind."""
	left = set()
	for meta, row in bundle.with_rows(record):
		for df in meta.get_link_fields():
			if df.options == PERSON and row.get(df.fieldname):
				left.add(row[df.fieldname])
				row[df.fieldname] = None
	for df in frappe.get_meta(record["doctype"]).get_table_fields():
		needs = [f.fieldname for f in frappe.get_meta(df.options).get_link_fields() if f.options == PERSON and f.reqd]
		if needs and record.get(df.fieldname):
			record[df.fieldname] = [row for row in record[df.fieldname] if all(row.get(f) for f in needs)]
	return left | _PEOPLE.get(record["doctype"], lambda r: set())(record)


def _workflow_people(record):
	"""Each node setting the registry declares as a Link to a person, cleared; its own publish check then asks for it."""
	left = set()
	for node in record["nodes"]:
		config = registry.config_of(node)
		named = [f["name"] for f in (registry.NODE_TYPES.get(node.get("node_type")) or {}).get("config") or []
		         if f.get("link") == PERSON and isinstance(config.get(f["name"]), str) and config[f["name"]]
		         and not refs.is_reference(config[f["name"]])]
		if named:
			left |= {config.pop(name) for name in named}
			node["config_json"] = frappe.as_json(config)
	return left


_PEOPLE = {"CRM Workflow": _workflow_people}


# -- where a record names another record or a grain value ---------------------

def _cells(record):
	"""(row, key, doctype, axis, listed) for every Link cell, plus each cell a record keeps in its own JSON."""
	cells = [(row, df.fieldname, df.options, None, False)
	         for row_meta, row in bundle.with_rows(record) for df in row_meta.get_link_fields()]
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
