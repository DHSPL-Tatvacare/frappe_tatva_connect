"""UNIQUE (user, reference_doctype, reference_name) on CRM Record Access — the table's whole point.

IT IS THE ACCESS PATH. The only read this table ever serves is "the records this set of users may see":
`select reference_name where reference_doctype = ? and user in (...)`. With `user` leading, one user's
grants are contiguous and the read is a seek plus a short range; `reference_doctype` and
`reference_name` ride in the leaf so the index answers the query without touching a row at all. That
covering property is what turns a 169,733-row scan into O(rows the user may see).

IT IS ALSO THE CORRECTNESS GUARD. A permission index that can hold the same grant twice will drift —
one delete leaves a duplicate behind and the user keeps a record they lost. UNIQUE makes the row
identity the grant itself, so `sync` and the backfill are both idempotent by construction rather than
by remembering to be.

Composite, so a doctype JSON cannot express it — and composite is also why it survives: frappe's schema
sync only ever drops single-column indexes (database/schema.py:311 via get_column_index, which returns
nothing for an index with a second column).

Runs BEFORE backfill_record_access in patches.txt, because that patch inserts with
ignore_duplicates=True and leans on this index to make a re-run safe. Idempotent (has_index guard).
Also in schema_setup._STEPS.
"""
import frappe

from tatva_connect.access.record_access import DOCTYPE

_INDEX = "ix_record_access_user_ref"
_COLUMNS = ("user", "reference_doctype", "reference_name")


def execute():
	if not frappe.db.table_exists(DOCTYPE):
		return
	table = f"tab{DOCTYPE}"
	if frappe.db.has_index(table, _INDEX):
		return
	try:
		frappe.db.add_unique(DOCTYPE, list(_COLUMNS), constraint_name=_INDEX)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"record_access: index {_INDEX} failed")
