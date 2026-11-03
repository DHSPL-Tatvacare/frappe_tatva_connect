"""Helpdesk's list data with the titles a grain-keyed master shows, so a list cell and a group header read a label, never a composite key."""
import frappe

from tatva_connect.api.list_link_titles import titles_for


@frappe.whitelist()
def get_list_data(**kwargs):
	"""Stock `helpdesk.api.doc.get_list_data`, plus `_link_titles` ({target}::{value} -> title) for its Link cells and titled group headers."""
	from helpdesk.api.doc import get_list_data as stock

	result = stock(**kwargs)
	doctype = kwargs.get("doctype")
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
