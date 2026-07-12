# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""No transactional doctype mints its name from an application counter.

A `naming_series` name is minted by incrementing ONE row in `tabSeries`, and that row's lock is held
until the transaction commits. Concurrent creates therefore queue on it and the database breaks the
pile-up with a deadlock: on a 32-way burst of lead creates, 1 of 32 survived and 31 died with
QueryDeadlockError. The partner API turned that into 124 HTTP 500s under load. With hash naming, all
32 survive and the 500s are zero.

`autoincrement` is the one permitted exception. Its id comes from MariaDB's OWN sequence, which
releases its lock immediately rather than holding it to commit, so it does not deadlock — 15,980
CRM Tasks were written through it with none. Its primary key is a `bigint`, so it could not hold a
hash without rewriting the column and every foreign key that points at it; it is left alone.

Masters are not touched. A master's name IS its identity (`Telephony::Acefone::calls`,
`GoodFlip Care::Anaya::Nivolumab`, `Referral`), and the composite `::` key is an invariant.

CRM Call Log is not touched either, though it looks like a candidate. Its `field:id` name is the
TELEPHONY PROVIDER's own call id — the webhook overwrites a placeholder with the real one so a later
CDR can be matched back to the row. That is an external key, not a name we are free to mint.

After this patch, `tabSeries` has no users.
"""
import frappe

# The only two doctypes still minting a name from tabSeries. Both already carry a varchar primary
# key, so nothing is altered: the naming rule changes, the column does not.
HASH_NAMED = ("CRM Lead", "CRM Deal")

# Undo damage from an earlier revision of this patch, which set these to hash without noticing their
# primary key is a bigint — a hash string does not fit an integer column, so every insert failed.
RESTORE_AUTOINCREMENT = (
	"CRM Task",
	"CRM Automation Run Log",
	"CRM Automation Resume",
	"CRM View Settings",
)


def _set(doctype, autoname, naming_rule):
	for prop, value in (("autoname", autoname), ("naming_rule", naming_rule)):
		frappe.make_property_setter(
			{
				"doctype": doctype,
				"doctype_or_field": "DocType",
				"property": prop,
				"value": value,
				"property_type": "Data",
			},
			is_system_generated=False,
		)


def execute():
	for doctype in RESTORE_AUTOINCREMENT:
		if not frappe.db.exists("DocType", doctype):
			continue
		if frappe.db.get_value("DocType", doctype, "autoname") != "autoincrement":
			_set(doctype, "autoincrement", "Autoincrement")
			frappe.db.set_value("DocType", doctype, "autoname", "autoincrement", update_modified=False)
			frappe.db.set_value("DocType", doctype, "naming_rule", "Autoincrement", update_modified=False)
			print(f"  {doctype}: restored to autoincrement (its primary key is a bigint)")

	for doctype in HASH_NAMED:
		if not frappe.db.exists("DocType", doctype):
			continue
		current = frappe.db.get_value("DocType", doctype, "autoname")
		if current == "hash":
			continue
		_set(doctype, "hash", "Random")
		frappe.db.set_value("DocType", doctype, "autoname", "hash", update_modified=False)
		frappe.db.set_value("DocType", doctype, "naming_rule", "Random", update_modified=False)
		print(f"  {doctype}: {current} -> hash (no tabSeries counter)")

	frappe.db.commit()
	frappe.clear_cache()
