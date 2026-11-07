"""Helpdesk's lists read a grain-keyed master by its label: cells and group headers show it, and a picked label matches every key carrying it."""
import frappe

from tatva_connect.api.list_link_titles import titles_for
from tatva_connect.list_engine.engine import read_label


@frappe.whitelist()
def get_list_data(**kwargs):
	"""Stock `helpdesk.api.doc.get_list_data`, plus `_link_titles` ({target}::{value} -> title) for its Link cells and titled group headers."""
	from helpdesk.api.doc import get_list_data as stock

	doctype = kwargs.get("doctype")
	for key in ("filters", "default_filters"):  # native merges a saved view's default_filters into filters, so both are read
		if doctype and kwargs.get(key):
			kwargs[key] = _labels_to_keys(doctype, kwargs[key])
	result = frappe.call(stock, **kwargs)  # frappe passes stock only the arguments it declares, as it does when stock is called directly
	if not doctype:
		return result
	links = {df.fieldname: df.options for df in frappe.get_meta(doctype).fields if df.fieldtype == "Link"}
	group = result.get("group_by_field") or {}
	wanted = {}
	for row in result.get("data") or []:
		for fieldname, target in links.items():
			if row.get(fieldname):
				wanted.setdefault(target, set()).add(row[fieldname])
	group_target = links.get(group.get("name")) if isinstance(group, dict) else None
	for option in (group.get("options") or []) if group_target else []:
		if isinstance(option, dict) and option.get("value"):
			wanted.setdefault(group_target, set()).add(option["value"])
	titles = {f"{target}::{value}": title for target, values in wanted.items() for value, title in titles_for(target, values).items()}
	for option in (group.get("options") or []) if group_target else []:
		if isinstance(option, dict) and option.get("value"):
			option["label"] = titles.get(f"{group_target}::{option['value']}") or option.get("label")
	result["_link_titles"] = titles
	return result


@frappe.whitelist()
def get_filterable_fields(**kwargs):
	"""Stock `helpdesk.api.doc.get_filterable_fields`, each Link told its filter query as the CRM's are (`task_lenses.link_queries`); the field stays a Link, so a condition builder still saves keys."""
	from helpdesk.api.doc import get_filterable_fields as stock

	from tatva_connect.api.task_lenses import link_queries

	return link_queries(frappe.call(stock, **kwargs))


def _labels_to_keys(doctype, filters):
	"""A filter picked by a label runs on every key carrying it, read by the CRM list's own `read_label`; any other filter is left as it came."""
	filters = frappe.parse_json(filters) if isinstance(filters, str) else filters
	if isinstance(filters, dict):
		return {field: read_label(doctype, field, cond) for field, cond in filters.items()}
	out = []
	for row in filters or []:
		if isinstance(row, list | tuple) and len(row) in (3, 4):
			*head, operator, value = row
			row = [*head, *read_label(doctype, row[-3], [operator, value])]
		out.append(row)
	return out
