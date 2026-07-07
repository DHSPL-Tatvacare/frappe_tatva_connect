# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.automation.subjects import is_subject


class CRMAutomationField(Document):
	"""The fail-closed field allowlist for the automation engine — one table, two capabilities
	(can_watch / can_set). Ships EMPTY: a field not listed here can neither be watched nor set.

	validate() makes the row unambiguous: at least one capability; the field must really exist; watch
	is grain-independent and subject-only (so grain/child columns only ever mean set-scope); an upsert
	key implies a child set. This replaces both `CRM Automatable Field` (write) and
	`CRM Automation Watchable Field` (read)."""

	def validate(self):
		# Structural guards first (cheap, no meta lookup), then confirm the field really exists.
		self._require_a_capability()
		self._guard_watch_shape()
		self._guard_row_key()
		self._require_real_field()

	def _require_real_field(self):
		if not frappe.db.exists("DocType", self.doctype_name):
			frappe.throw(_("DocType {0} does not exist.").format(frappe.bold(self.doctype_name)))
		# For a child-row target the field lives on the child doctype of the parent's Table field;
		# otherwise on the doctype itself. Resolve the right meta so a typo is caught at config time.
		target_dt = self.doctype_name
		if self.child_table_field:
			tf = frappe.get_meta(self.doctype_name).get_field(self.child_table_field)
			if not tf or tf.fieldtype != "Table":
				frappe.throw(
					_("{0} is not a child table on {1}.").format(
						frappe.bold(self.child_table_field), frappe.bold(self.doctype_name)
					)
				)
			target_dt = tf.options
		if not frappe.get_meta(target_dt).has_field(self.fieldname):
			frappe.throw(
				_("{0} has no field {1}.").format(frappe.bold(target_dt), frappe.bold(self.fieldname)),
				title=_("Unknown fieldname"),
			)

	def _require_a_capability(self):
		if not (self.can_watch or self.can_set):
			frappe.throw(
				_("A field row must be watchable, settable, or both — tick Can Watch or Can Set."),
				title=_("No capability"),
			)

	def _guard_watch_shape(self):
		"""Watch is grain-independent, parent-only, and subject-only — so grain/child columns can only
		ever mean set-scope (no ambiguity), and you can only watch a doctype the engine resolves to a
		lead."""
		if not self.can_watch:
			return
		if self.child_table_field:
			frappe.throw(_("A child-table field cannot be watched — clear Child Table Field or untick Can Watch."))
		if self.vertical or self.group or self.program:
			frappe.throw(_("Watch is grain-independent — clear the grain axes, or use a separate Can Set row."))
		if not is_subject(self.doctype_name):
			frappe.throw(
				_("{0} is not a watch subject (CRM Lead / CRM Task). A new subject needs a resolver in automation.subjects first.").format(
					frappe.bold(self.doctype_name)
				),
				title=_("Not a watch subject"),
			)

	def _guard_row_key(self):
		if self.is_row_key and not (self.can_set and self.child_table_field):
			frappe.throw(_("Is Row Key applies only to a settable child-table field."))
