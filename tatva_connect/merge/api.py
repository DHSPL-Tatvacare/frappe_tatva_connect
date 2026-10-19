"""Merge one record into another through `rename_doc(merge=True)`.

Native already moves every Link, Dynamic Link, assignment, attachment and version onto the kept record,
writes "merged X into Y" on it and deletes the other — gated by write on the one and delete on the other.
What native drops is the deleted record's own values and child rows: the person picks which come across.
"""
import frappe
from frappe import _
from frappe.desk.form.linked_with import get as linked_records
from frappe.model import data_fieldtypes, table_fields
from frappe.model.rename_doc import rename_doc

from tatva_connect.taxonomy import grain
from tatva_connect.whatsapp.phone import lookup_e164

CONTACT, LEAD = "Contact", "CRM Lead"
# Rows the system writes about a lead's own history; they describe the record that is going, so they go with it.
SYSTEM_TABLES = {LEAD: {"status_change_log", "rolling_responses", "custom_lead_activity_metrics"}}


def _pair(doctype, keep, drop):
	"""The two records, refused unless both exist, differ, may be merged by this user, and share a grain."""
	if doctype not in (CONTACT, LEAD):
		frappe.throw(_("{0} records cannot be merged").format(_(doctype)))
	if keep == drop:
		frappe.throw(_("Pick a different record to merge into"))
	kept, dropped = frappe.get_doc(doctype, keep), frappe.get_doc(doctype, drop)
	kept.check_permission("write")
	dropped.check_permission("delete")
	if doctype == LEAD:
		_refuse_other_grain(keep, drop)
	return kept, dropped


def _refuse_other_grain(keep, drop):
	if not all(map(grain.same, grain.of(LEAD, keep), grain.of(LEAD, drop))):
		frappe.throw(_("Leads from different programs cannot be merged"), title=_("Different grain"))


def before_rename(doc, method=None, old=None, new=None, merge=False):
	"""CRM Lead before_rename: a merge, from this engine or Desk's own Rename, stays in one grain and ends the dropped lead's journeys."""
	if not merge:
		return
	from tatva_connect.workflow_engine.interpreter import stop_for_subject

	_refuse_other_grain(new, old)
	# Native would move the dropped lead's live journeys onto the kept one, which may already run the same workflow.
	stop_for_subject(LEAD, old, _("Merged into {0}").format(new))


def _fields(kept, dropped):
	"""Each editable field the dropped record holds a value for that the kept one does not share."""
	return [
		{"fieldname": df.fieldname, "label": _(df.label), "keep": kept.get(df.fieldname), "drop": dropped.get(df.fieldname)}
		for df in kept.meta.fields
		if df.fieldtype in data_fieldtypes and df.fieldtype not in table_fields
		and not (df.read_only or df.hidden or df.is_virtual)
		and dropped.get(df.fieldname) not in (None, "") and dropped.get(df.fieldname) != kept.get(df.fieldname)
	]


def _values(row):
	"""A child row as its data, without the bookkeeping every row carries."""
	return {df.fieldname: row.get(df.fieldname) for df in row.meta.fields if df.fieldtype in data_fieldtypes}


def _key(row):
	"""What makes two rows the same: a contact's phone in any spelling or email in any case, else every value."""
	if row.doctype == "Contact Phone":
		return lookup_e164(row.phone) or row.phone
	if row.doctype == "Contact Email":
		return (row.email_id or "").lower()
	return _values(row)


def _new_rows(kept, dropped, table):
	"""The dropped record's rows in `table` that the kept record does not already hold."""
	held = [_key(row) for row in kept.get(table)]
	return [row for row in dropped.get(table) if _key(row) not in held]


def _tables(kept, dropped):
	"""Each child table the dropped record has rows in that the kept one lacks."""
	skip = SYSTEM_TABLES.get(kept.doctype, set())
	return [
		{"fieldname": df.fieldname, "label": _(df.label), "rows": len(rows)}
		for df in kept.meta.get_table_fields()
		if df.fieldname not in skip and (rows := _new_rows(kept, dropped, df.fieldname))
	]


def _carry(kept, row):
	"""Add one row to the kept record; a contact's phone or email joins through Contact's own API, never as a second primary."""
	if row.doctype == "Contact Phone":
		kept.add_phone(row.phone)
	elif row.doctype == "Contact Email":
		kept.add_email(row.email_id)
	else:
		kept.append(row.parentfield, _values(row))


@frappe.whitelist()
def preview(doctype: str, keep: str, drop: str) -> dict:
	"""What a merge of `drop` into `keep` would move, and the values and rows the person may bring across."""
	kept, dropped = _pair(doctype, keep, drop)
	moves = {_(dt): len(rows) for dt, rows in linked_records(doctype, drop).items() if rows}
	return {"fields": _fields(kept, dropped), "tables": _tables(kept, dropped), "moves": moves}


@frappe.whitelist(methods=["POST"])
def merge(doctype: str, keep: str, drop: str, take: list | str | None = None, carry: list | str | None = None) -> str:
	"""Merge `drop` into `keep`: the picked values and rows come across, then native moves every link and deletes `drop`."""
	kept, dropped = _pair(doctype, keep, drop)
	picked_fields, picked_tables = set(frappe.parse_json(take) or []), set(frappe.parse_json(carry) or [])
	# Only what the preview offered may be written, so a crafted call cannot reach a read-only or hidden field.
	if not (picked_fields <= {f["fieldname"] for f in _fields(kept, dropped)}
	        and picked_tables <= {t["fieldname"] for t in _tables(kept, dropped)}):
		frappe.throw(_("Only the fields and tables the merge preview offers can be brought across"))
	values = {fieldname: dropped.get(fieldname) for fieldname in picked_fields}
	rows = [row for table in picked_tables for row in _new_rows(kept, dropped, table)]
	rename_doc(doctype, drop, keep, merge=True)
	kept = frappe.get_doc(doctype, keep)
	kept.update(values)
	for row in rows:
		_carry(kept, row)
	kept.save()
	return keep
