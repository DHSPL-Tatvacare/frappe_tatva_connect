# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""A lead field label is the plain column name; the section is presentation, not part of the label.

Some rows were seeded with the section baked into the label (`Acquisition — UTM Source`, `Lead — Status`),
so the Data tab — which already shows the section as its own header — printed the section twice, and the
50 prefixed rows sat inconsistently beside 163 plain ones. The flat surfaces that DO need the section
(Smart Views picker and results grid) compose `section.title — label` at render now, so the stored label
carries the column name alone. End state: no label keeps a leading `<prefix> — `.

Generic and idempotent: strip up to and including the first ` — ` (an em-dash with spaces, which never
appears inside a real column name — those use `-`, `:`, parens). A plain label has no ` — ` and is left
untouched, so this is safe to replay every deploy. Assumes nothing about which rows were prefixed.
"""
import frappe

DT = "CRM Lead API Field"


def execute():
	for name, label in frappe.get_all(DT, fields=["name", "label"], as_list=True):
		_head, sep, tail = (label or "").partition(" — ")
		if sep and tail and tail != label:
			frappe.db.set_value(DT, name, "label", tail, update_modified=False)
	frappe.db.commit()
