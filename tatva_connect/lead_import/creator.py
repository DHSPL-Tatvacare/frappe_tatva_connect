# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Desk import's per-row closures — live and dry — bound to the import's contract, never the session user."""
import frappe

from tatva_connect.api import partner
from tatva_connect.lead_sync import contract as contract_brain


def grain_of(contract):
	"""The grain descriptor `_upsert_one` reads — the shape intake and lead_sync build."""
	return frappe._dict(source=contract.source, vertical=contract.vertical,
	                    crm_group=contract.crm_group, program=contract.program)


def stage_row(row, field_keys):
	"""One sheet row as a partner payload, placed by `contract.stage`."""
	item = {}
	for header, field_key in field_keys.items():
		value = row.get(header)
		if value is None or (isinstance(value, str) and not value.strip()):
			continue  # an empty cell is "not sent", never "erase this"
		contract_brain.stage(item, field_key, value.strip() if isinstance(value, str) else value)
	return item


def bound_grain(import_doc):
	"""The grain every row of this import is written and scoped to: the contract's, its blanks filled by the import."""
	mp = grain_of(import_doc.contract_doc())
	mp.program = mp.program or import_doc.get("program")  # a contract that fixes either axis wins
	mp.source = mp.source or import_doc.get("source")
	return mp


def _bind(import_doc):
	"""Everything a row needs, resolved once per job."""
	contract = import_doc.contract_doc()
	parent_fields, child_allow = partner._split_keys(contract_brain.allowed_field_keys(contract))
	one = partner.bulk_creator(frappe.session.user, bound_grain(import_doc), False, parent_fields, child_allow,
	                           contract_brain.allowed_programs(contract))
	return one, import_doc.field_key_map()


def live_creator(import_doc):
	"""The live run: each row through the partner API's own create closure."""
	one, field_keys = _bind(import_doc)

	def create(index, row):
		return one(index, stage_row(row, field_keys))

	return create


def deleter(import_doc):
	"""Stop's delete-one for this import: the partner API's own delete, fenced to the grain its rows were written to."""
	grain = bound_grain(import_doc)
	return lambda name: partner._delete_one(name, grain, False)


def dry_creator(import_doc):
	"""The dry run: the live closure inside a savepoint that is always rolled back."""
	one, field_keys = _bind(import_doc)

	def check(index, row):
		savepoint = f"tc_dry_{index}"
		frappe.db.savepoint(savepoint)
		try:
			result = one(index, stage_row(row, field_keys))
		finally:
			frappe.db.rollback(save_point=savepoint)
		result["action"] = "validated"
		return result

	return check
