# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

# Axes that form the composite-unique key + the autoname `format:` string.
_KEY_FIELDS = ("program", "stage")


def _canonicalize(doc):
	# The name is `format:{program}::{stage}`, built BEFORE validate (set_new_name
	# runs at insert ahead of validate). program/stage are both reqd so neither is
	# NULL, but strip surrounding whitespace so " New " and "New" can't diverge in
	# the rendered name. Run from before_insert (pre-name) AND validate (edit path).
	for f in _KEY_FIELDS:
		v = doc.get(f)
		if isinstance(v, str):
			doc.set(f, v.strip() or None)


class CRMLeadStage(Document):
	def before_insert(self):
		_canonicalize(self)

	def validate(self):
		_canonicalize(self)

		# Two stages with the IDENTICAL (program, stage) pair would collide on the
		# `format:` name at insert; this also blocks a tuple-duplicate produced by an
		# in-place edit (format: fires only at insert). Compare in Python so the guard
		# is uniform with the routing dup-guards.
		def _pair(d):
			return ((d.program or ""), (d.stage or ""))

		mine = _pair(self)
		for other in frappe.get_all(
			"CRM Lead Stage",
			filters={"name": ["!=", self.name or ""]},
			fields=["name", "program", "stage"],
		):
			if _pair(other) == mine:
				frappe.throw(
					_(
						"A stage with the same Program / Stage already exists ({0}). "
						"Each Program / Stage combination must be unique."
					).format(other.name),
					title=_("Duplicate stage"),
				)

	def before_save(self):
		"""Derive the readable display_label shown in the Lead's Stage picker:
		'Program / Main / Substage' for a substage, else 'Program / Stage'."""
		if self.substage_of:
			parent_stage = frappe.db.get_value("CRM Lead Stage", self.substage_of, "stage")
			self.display_label = "{0} / {1} / {2}".format(self.program, parent_stage, self.stage)
		else:
			self.display_label = "{0} / {1}".format(self.program, self.stage)
