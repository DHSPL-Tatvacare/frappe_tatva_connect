# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Give an already-written request-log error a code, so no `not in` filter can hide it.

THE DEFECT. Every API-error figure filters `error_code not in ('not_found', 'cannot_delete',
'rate_limited')` to drop the refusals nobody chases. Frappe compiles a `not in` so that it EXCLUDES a
blank, so an error the handler never classified matched no filter at all and was invisible on all six
surfaces at once — cards, charts and the endpoint breakdown. Measured on the bench this was found on:
7 of 246.

`capture.py` now stamps `UNCLASSIFIED` at write time, so this only repairs the rows already written.
Idempotent: it touches a row exactly once, because after it runs no error row has a blank code.
"""
import frappe

from tatva_connect.observability.capture import UNCLASSIFIED


def execute():
	frappe.db.sql(  # sqli-ok: constant table and column, single bound value
		"UPDATE `tabCRM API Request Log` SET error_code = %(code)s "
		"WHERE is_error = 1 AND COALESCE(error_code, '') = ''",
		{"code": UNCLASSIFIED},
	)
