"""Read the contract a lead source is created against — grain and ticked field_keys, nothing local."""
import frappe

from tatva_connect.api.partner import _catalog

CONTRACT = "CRM Lead API Mapping"


def contract_of(source):
	"""The contract this source links to; a source without one can never create a lead."""
	name = source.get("api_mapping")
	if not name:
		frappe.throw(
			frappe._("{0} has no Contract. Its leads have no grain and no rule can route them.").format(source.name),
			title=frappe._("Contract required"),
		)
	contract = frappe.get_cached_doc(CONTRACT, name)
	if not contract.enabled:
		frappe.throw(
			frappe._("The contract {0} is disabled.").format(name), title=frappe._("Contract disabled")
		)
	return contract


def allowed_field_keys(contract):
	"""The ticked catalog keys, or the full catalog when the grid is empty — the partner rule, unchanged."""
	picked = {row.field for row in (contract.allowed_fields or [])}
	keys = _catalog()["keys"]
	if not picked:
		return set(keys)
	picked.add("lead:mobile_no")  # identity is never optional
	return {k for k in keys if k in picked}


def allowed_programs(contract):
	return [row.program for row in (contract.allowed_programs or [])]


def stage(item, field_key, value):
	"""Place a value under the shape _collect expects: parent fields flat, child fields under their table."""
	section, _, fieldname = field_key.partition(":")
	child_table = _catalog()["section_child"].get(section)
	if child_table:
		item.setdefault(child_table, [{}])[0][fieldname] = value
	else:
		item[fieldname] = value
