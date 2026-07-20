# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The per-record closures for a Desk lead import — one that writes, one that writes and rolls back.

The grain comes from the IMPORT'S CONTRACT, never from the session user. A Desk operator is a System
Manager, and the partner path would hand that caller `is_sysmgr=True` with no mapping, which admits
routing straight off the payload — a spreadsheet could then choose its own vertical. Building a _dict
grain descriptor is the seam intake and Facebook sync already use; `_upsert_one` reads only
`.source/.vertical/.crm_group/.program` off it, so a _dict is a complete substitute for a mapping doc.

Placement is `contract.stage()`'s job. A column names a `field_key` and the section decides which of the
four shapes it lands in, so this module holds no map of its own and no child table name appears here.
"""
import frappe

from tatva_connect.api import partner
from tatva_connect.lead_sync import contract as contract_brain


def grain_of(contract):
	"""The grain descriptor `_upsert_one` reads — the shape intake.py and lead_sync/source.py build."""
	return frappe._dict(source=contract.source, vertical=contract.vertical,
	                    crm_group=contract.crm_group, program=contract.program)


def stage_row(row, field_keys):
	"""One sheet row -> the payload shape, placed by the ONE placement brain."""
	item = {}
	for header, field_key in field_keys.items():
		value = row.get(header)
		if value is None or (isinstance(value, str) and not value.strip()):
			continue  # an empty cell is "not sent", never "erase this"
		contract_brain.stage(item, field_key, value.strip() if isinstance(value, str) else value)
	return item


def _bind(import_doc):
	"""Everything a row needs, resolved once per job rather than once per row."""
	contract = frappe.get_cached_doc("CRM Lead API Mapping", import_doc.contract)
	parent_fields, child_allow = partner._split_keys(contract_brain.allowed_field_keys(contract))
	mp = grain_of(contract)
	if import_doc.get("program"):
		mp.program = import_doc.program
	one = partner.bulk_creator(frappe.session.user, mp, False, parent_fields, child_allow,
	                           contract_brain.allowed_programs(contract))
	return one, import_doc.field_key_map()


def live_creator(import_doc):
	"""Stage the row, then hand it to the SAME create closure the partner API and the worker use."""
	one, field_keys = _bind(import_doc)

	def create(index, row):
		return one(index, stage_row(row, field_keys))

	return create


def dry_creator(import_doc):
	"""Validation: the live path, written and rolled back, so what passes here is what will be written."""
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
