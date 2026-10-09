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

		self._parent_in_same_program()
		self._one_sale_stage_per_program()
		# A stage with sub-stages is a heading, so a rep picks one of its sub-stages instead.
		if frappe.db.exists("CRM Lead Stage", {"substage_of": self.name}):
			self.selectable = 0

	def _parent_in_same_program(self):
		if not self.substage_of:
			return
		parent_program = frappe.db.get_value("CRM Lead Stage", self.substage_of, "program")
		if parent_program != self.program:
			frappe.throw(
				_("Pick a parent stage from {0}, because {1} belongs to {2}.").format(
					self.program, self.substage_of, parent_program
				),
				title=_("Parent stage is in another program"),
			)

	def _one_sale_stage_per_program(self):
		if not self.is_conversion_point:
			return
		other = frappe.db.get_value(
			"CRM Lead Stage",
			{"program": self.program, "is_conversion_point": 1, "name": ["!=", self.name or ""]},
			"name",
		)
		if other:
			frappe.throw(
				_("Untick the sale on {0} first, because a program has only one sale stage.").format(other),
				title=_("One sale stage per program"),
			)

	def on_update(self):
		# The parent of a new sub-stage becomes a heading, so it stops being pickable.
		if self.substage_of and frappe.db.get_value("CRM Lead Stage", self.substage_of, "selectable"):
			frappe.db.set_value("CRM Lead Stage", self.substage_of, "selectable", 0)

	def before_save(self):
		"""The human label shown in the UI — just the clean stage name (e.g. "New Patient").
		The composite `::` name is the PK (DB/code only); the UI reads this title via
		title_field + show_title_field_in_link, so `::` never reaches the screen."""
		self.display_label = self.stage
