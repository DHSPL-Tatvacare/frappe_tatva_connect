# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""An operator nickname for a value the CRM already holds — the one thing the vocabulary cannot derive.

The row stores a POINTER, not a spelling: `resolve` hands back the master and the record behind what was typed,
and the Dynamic Link is what makes a later rename of that master follow instead of quietly dropping the alias.
`resolves_to` is re-stamped from that resolution on every save, so the form shows the canonical value rather than
whatever was typed. Named by hash rather than by `term`: the name would be minted from the raw term before
validate normalises it, leaving the record and its own field spelt differently from the first save.
"""
import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.search import vocabulary
from tatva_connect.search.index import normalise


class CRMSearchAlias(Document):
	def validate(self):
		# One tokeniser for both sides: the term is stored exactly as a typed query will be read.
		self.term = normalise(self.term)
		if len(self.term) < vocabulary.MIN_TERM_LEN:
			frappe.throw(_("A term of fewer than {0} letters matches every query and narrows nothing.").format(vocabulary.MIN_TERM_LEN))
		if vocabulary.resolve(self.term):
			frappe.throw(_("{0} is already a value in its own right, so an alias for it would mean two things at once and match neither.").format(frappe.bold(self.term)))
		self._point_at(vocabulary.resolve(self.resolves_to))

	def _point_at(self, matches):
		# Exactly one reading is stamped; nothing is guessed, and the operator is told which case they are in.
		if not matches:
			near = vocabulary.suggest(self.resolves_to)
			hint = _("Did you mean: {0}?").format(", ".join(near)) if near else _("Only a stage, status, product line, group, program or person may be aliased.")
			frappe.throw(_("{0} is not a value this CRM holds. {1}").format(frappe.bold(self.resolves_to), hint))
		if len(matches) > 1:
			where = ", ".join(sorted(m["doctype"] for m in matches))
			frappe.throw(_("{0} is held in more than one place ({1}), so which one is meant cannot be decided here.").format(frappe.bold(self.resolves_to), where))
		hit = matches[0]
		self.target_doctype, self.target_name, self.resolves_to = hit["doctype"], hit["name"], hit["value"]

	def on_update(self):
		vocabulary.reload()

	def on_trash(self):
		vocabulary.reload()
