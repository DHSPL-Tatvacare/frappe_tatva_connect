# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

# Axes that may be blank and so must be canonicalised before the name is built.
# task_type is reqd (never blank) but is still part of the key for the dup-guard.
_BLANK_AXES = ("vertical", "psp_group", "program")


def _canonicalize(doc):
	# The name is `format:{task_type}::{vertical}::{psp_group}::{program}`, built
	# BEFORE validate (set_new_name runs at insert ahead of validate), so blank axes
	# must be one sentinel — NULL, never "" — before the name is built. Else a row
	# with NULL and a row with "" diverge yet collapse in the rendered name. Run from
	# before_insert (pre-name) AND validate (covers the edit path).
	for f in _BLANK_AXES:
		if not (doc.get(f) or "").strip():
			doc.set(f, None)


class CRMTaskChecklistTemplate(Document):
	def before_insert(self):
		_canonicalize(self)

	def validate(self):
		_canonicalize(self)

		# Two templates with the IDENTICAL (task_type, vertical, group, program) quad
		# are equally specific -> ambiguous which checklist applies. `format:` only
		# enforces uniqueness at insert, so this also blocks a tuple-duplicate created
		# by an in-place edit. Compare in Python (canonical None) so blank Link axes
		# match uniformly. Mirrors the routing dup-guards.
		def _key(d):
			return ((d.task_type or ""), (d.vertical or ""), (d.psp_group or ""), (d.program or ""))

		mine = _key(self)
		for other in frappe.get_all(
			"CRM Task Checklist Template",
			filters={"name": ["!=", self.name or ""]},
			fields=["name", "task_type", "vertical", "psp_group", "program"],
		):
			if _key(other) == mine:
				frappe.throw(
					_(
						"A checklist template for the same Task Type / Product Line / Group / "
						"Program already exists ({0}). Each combination must be unique."
					).format(other.name),
					title=_("Duplicate checklist template"),
				)
