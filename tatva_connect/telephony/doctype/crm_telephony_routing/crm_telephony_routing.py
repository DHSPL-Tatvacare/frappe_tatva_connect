# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect import phone
from tatva_connect.telephony import envelope as env

# Axes that form the composite-unique key + the autoname `format:` string.
_KEY_FIELDS = ("vertical", "psp_group", "program")

DID_CHILD = "CRM Telephony Routing DID"


def _canonicalize(doc):
	# The name is `format:{vertical}::{psp_group}::{program}`, built BEFORE validate
	# (set_new_name runs at insert ahead of validate), so blank axes must be one
	# sentinel — NULL, never "" — before the name is built. Else (N,NULL,NULL) and
	# (N,'','') diverge in the row but collapse in the rendered name. Run from
	# before_insert (pre-name) AND validate (covers the edit path).
	for f in _KEY_FIELDS:
		if not (doc.get(f) or "").strip():
			doc.set(f, None)


class CRMTelephonyRouting(Document):
	def before_insert(self):
		_canonicalize(self)

	def validate(self):
		_canonicalize(self)

		# No global default: a rule must scope at least one axis. An all-blank rule would
		# match every lead (score 0) and silently become a catch-all.
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
			"CRM Telephony Routing",
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

		self._validate_dids()

	def _validate_dids(self):
		"""Normalize each number, and hold the rule that a number belongs to exactly one grain.

		The number was the primary key of its own doctype, which enforced this for free. It is a child
		row now, so it is enforced here instead: the same DID on two rules would attribute one call to
		two grains, and which one won would depend on row order.
		"""
		seen = {}
		for row in self.dids or []:
			digits = phone.match_digits(row.did_number, last=10)
			if not digits:
				frappe.throw(
					_("{0} is not a full phone number. A DID needs at least {1} digits.").format(
						frappe.bold(row.did_number or ""), env.PHONE_MIN_DIGITS
					),
					title=_("Invalid DID"),
				)
			if digits in seen:
				frappe.throw(
					_("DID {0} is listed twice on this rule.").format(frappe.bold(digits)),
					title=_("Duplicate DID"),
				)
			seen[digits] = row
			row.did_number = digits

		if not seen:
			return

		clash = frappe.get_all(
			DID_CHILD,
			filters={
				"did_number": ["in", list(seen)],
				"parenttype": self.doctype,
				"parent": ["!=", self.name or ""],
			},
			fields=["did_number", "parent"],
			limit=1,
		)
		if clash:
			frappe.throw(
				_("DID {0} is already mapped to {1}. A number can belong to only one grain.").format(
					frappe.bold(clash[0].did_number), clash[0].parent
				),
				title=_("DID already mapped"),
			)
