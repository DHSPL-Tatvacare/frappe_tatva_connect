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


def stage(item, field_key, value, question=None, label=None, form=None):
	"""Place a value under the shape _collect expects: parent fields flat, child fields under their table,
	a key-value answer as its own row.

	A key-value answer is built HERE rather than by the caller: this is where the section is already
	resolved, and the section is what names the column each part of a row lands in. The identity is not
	set here at all — the row derives its own from the question on validate — so a caller can neither
	name a column nor invent an identity."""
	cat = _catalog()
	section, _, fieldname = field_key.partition(":")
	child_table = cat["section_child"].get(section)
	key_value = cat["section_key_value"].get(section)
	if key_value:
		item.setdefault(child_table, []).append({
			key_value.value_field: value,
			key_value.question_field: question,
			key_value.label_field: label,
			"form": form,
		})
	elif child_table:
		item.setdefault(child_table, [{}])[0][fieldname] = value
	else:
		item[fieldname] = value


def screening_key():
	"""The key a screening answer is staged under: the first key-value section. The question carries its own
	identity, so the key names only the section. None while no section declares itself key-value, so such an
	answer is dropped exactly as it was before one did — the seed decides, and code never re-decides it."""
	sections = list(_catalog()["section_key_value"])
	return f"{sections[0]}:" if sections else None
