# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

# Axes that form the composite-unique key + the autoname `format:` string.
_KEY_FIELDS = ("vertical", "psp_group", "program")


def _canonicalize(doc):
	# The name is `format:{vertical}::{psp_group}::{program}`, built BEFORE validate
	# (set_new_name runs at insert ahead of validate), so blank axes must be one
	# sentinel — NULL, never "" — before the name is built. Else (N,NULL,NULL) and
	# (N,'','') diverge in the row but collapse in the rendered name. Run from
	# before_insert (pre-name) AND validate (covers the edit path).
	for f in _KEY_FIELDS:
		if not (doc.get(f) or "").strip():
			doc.set(f, None)


class CRMWATIAccountRouting(Document):
	def before_insert(self):
		_canonicalize(self)

	def validate(self):
		_canonicalize(self)

		# No global default: a rule must scope at least one axis. An all-blank rule
		# would match every lead (score 0) and silently become a catch-all.
		if not (self.vertical or self.psp_group or self.program):
			frappe.throw(
				_(
					"Set at least one of Product Line / Group / Program. An all-blank rule "
					"would act as a global default, which is not allowed."
				),
				title=_("Invalid routing rule"),
			)

		# Two rules with the IDENTICAL (vertical, group, program) triple are equally
		# specific and could both match a lead -> ambiguous routing. `format:` only
		# enforces uniqueness at insert, so this also blocks a tuple-duplicate created
		# by an in-place edit. Compare in Python (canonical None) so empty Link axes
		# match uniformly.
		def _triple(d):
			return ((d.vertical or ""), (d.psp_group or ""), (d.program or ""))

		mine = _triple(self)
		for other in frappe.get_all(
			"CRM WATI Account Routing",
			filters={"name": ["!=", self.name or ""]},
			fields=["name", "vertical", "psp_group", "program"],
		):
			if _triple(other) == mine:
				frappe.throw(
					_(
						"A routing rule with the same Product Line / Group / Program already "
						"exists ({0}). Each combination must be unique."
					).format(other.name),
					title=_("Duplicate routing rule"),
				)
