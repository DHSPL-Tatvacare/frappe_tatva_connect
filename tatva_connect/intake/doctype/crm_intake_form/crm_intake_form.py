# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model import no_value_fields
from frappe.model.document import Document

from tatva_connect.intake import layers
from tatva_connect.intake.builder import LAYOUT_FIELDTYPES
from tatva_connect.intake.intake import target_doctype
from tatva_connect.taxonomy.normalize import normalize_field


class CRMIntakeForm(Document):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		# Frappe checks a Dynamic Link on insert before any hook runs, so the Source list exists from construction.
		if not self.get("source_doctype"):
			self._derive_source_doctype()

	def before_validate(self):
		self._derive_source_doctype()

	def _derive_source_doctype(self):
		"""Source picks from the list the target's own source field links to."""
		self.source_doctype = layers.source_doctype(layers.target_of(self))

	def validate(self):
		# M-2: normalize the form name (the display key) so variants never fork.
		normalize_field(self, "form_name")
		self._validate_target()
		self._validate_fields()
		self._validate_dependencies()
		self._validate_targets()
		self._validate_phone_mapping()

	# The section the form hides for a layer target; its fields are read off the layout, never listed.
	_LEAD_ONLY_SECTION = "routing_section"

	def _validate_target(self):
		"""A form's target is fixed once its submission table exists; a layer target keeps nothing from the section it hides."""
		before = self.get_doc_before_save()
		if before and self.web_form_doctype and layers.target_of(before) != layers.target_of(self):
			frappe.throw(_("The target cannot change once the form has its submission table. Create a new form instead."),
			             title=_("Target Is Fixed"))
		if layers.layer_of(self):
			for fieldname in self._lead_only_fields():
				self.set(fieldname, None)

	def _lead_only_fields(self):
		"""The value fields between the lead-only section break and the next one."""
		fields = self.meta.fields
		start = next(i for i, df in enumerate(fields) if df.fieldname == self._LEAD_ONLY_SECTION) + 1
		out = []
		for df in fields[start:]:
			if df.fieldtype in ("Section Break", "Tab Break"):
				break
			if df.fieldtype not in no_value_fields:
				out.append(df.fieldname)
		return out

	def _validate_fields(self):
		"""A Select field needs its option list (one per line) — else the published form
		renders an empty, unusable dropdown."""
		for m in self.mappings:
			if (m.get("fieldtype") or "Data").strip() == "Select" and not (m.get("options") or "").strip():
				frappe.throw(
					_("Field '{0}' is a Select but has no Options (one value per line).").format(
						m.source_field or "?"
					),
					title=_("Select Needs Options"),
				)

	def _validate_dependencies(self):
		"""A Depends On names another lookup question on this form and resolves to a real field on this question's list."""
		lookups = {layers.question_name(m) for m in self.mappings if (m.get("fieldtype") or "").strip() == "Link"}
		for m in self.mappings:
			parent = (m.get("depends_on_question") or "").strip()
			if not parent:
				continue
			if parent not in lookups or parent == layers.question_name(m):
				frappe.throw(_("'{0}' depends on '{1}', which is not another lookup question on this form.").format(
					m.source_field, parent), title=_("Invalid Depends On"))
			field = layers.matched_by(self, m)
			if not (field and frappe.get_meta(m.options).has_field(field)):
				frappe.throw(_("Set Matched By on '{0}': no field on {1} holds the '{2}' answer.").format(
					m.source_field, m.options, parent), title=_("Invalid Depends On"))

	def _validate_targets(self):
		"""Fail-closed mapping contract: a row that DECLARES a target_table must point at a
		field that exists on the doctype it resolves to (lead = CRM Lead; a child section via
		its CRM Lead Section row; `note` = a free-text Note title, not field-checked). A row with NO
		target_table is a web-form-only input or a layout field (Section/Column Break, HTML) and
		lands nothing on the lead — so it is not field-checked.

		Read from LIVE meta (frappe.get_meta) — never a baked field list — so a renamed or
		removed field is caught and the bad field is named. Only blocks on SAVE."""
		if layers.layer_of(self):
			return self._validate_layer_targets()
		for m in self.mappings:
			table, field = layers.target_pair(m)
			if not table:
				continue  # unmapped input / layout field — nothing lands on the lead
			self._validate_stores_a_value(m, table)
			if not field:
				frappe.throw(
					_("Field '{0}' maps to '{1}' but has no Target Field.").format(
						m.source_field or "?", table
					),
					title=_("Incomplete Mapping"),
				)
			if table == "note":
				continue
			dt = target_doctype(table)
			if not dt:
				frappe.throw(
					_("Unknown Target Table '{0}' (field '{1}').").format(table, m.source_field or "?"),
					title=_("Invalid Target"),
				)
			if not frappe.get_meta(dt).has_field(field):
				frappe.throw(
					_("Target field '{0}' does not exist on {1} (field '{2}').").format(
						field, dt, m.source_field or "?"
					),
					title=_("Invalid Target Field"),
				)
			self._validate_target_in_brain(m, table, field)

	def _validate_layer_targets(self):
		"""A layer form's question lands on its target or a related record, on a field that record offers."""
		target = layers.target_of(self)
		for m in self.mappings:
			table, field = layers.target_pair(m)
			if not table:
				continue
			self._validate_stores_a_value(m, table)
			if table not in layers.destinations(target):
				frappe.throw(_("'{0}' is not a record this form writes (field '{1}'). Allowed: {2}.").format(
					table, m.source_field or "?", ", ".join(layers.destinations(target))), title=_("Invalid Target"))
			if not any(f["fieldname"] == field for f in layers.fields_of(target, table)):
				frappe.throw(_("'{0}' is not a field this form may write on {1} (field '{2}').").format(
					field, table, m.source_field or "?"), title=_("Invalid Target Field"))

	def _validate_stores_a_value(self, m, table):
		"""A layout field is web-form furniture and gets no column on the submission table, so a target
		on one can never land — today it saves clean and the answer quietly goes nowhere. The set of
		layout types is the builder's ONE declaration, read here rather than copied."""
		fieldtype = (m.get("fieldtype") or "Data").strip()
		if fieldtype in LAYOUT_FIELDTYPES:
			frappe.throw(
				_("'{0}' is a {1} — it stores no answer, so it cannot map to '{2}'.").format(
					m.source_field or "?", fieldtype, table
				),
				title=_("Layout Field Cannot Map"),
			)

	def _validate_target_in_brain(self, m, table, field):
		"""Backstop for the grain-scoped picker: the target must be one the ONE mapping seam offers for
		this section AND this form's grain. The builder's dropdown is fed by that same call, so the two
		cannot drift — a hit here means an API or import write that never opened the builder.
		The message NAMES what is allowed, so it is actionable rather than a dead end."""
		from tatva_connect.intake.api import list_target_fields

		grain = ((self.custom_vertical or "").strip(), (self.custom_group or "").strip(),
		         (self.custom_current_program or "").strip())
		if not any(grain):
			frappe.throw(
				_("Set the Vertical / Group / Program before mapping fields — targets are grain-scoped."),
				title=_("Grain Required"),
			)
		offered = list_target_fields(table, self.name, *grain)
		if any(f["fieldname"] == field for f in offered):
			return
		allowed = ", ".join(f["fieldname"] for f in offered) or _("(none)")
		frappe.throw(
			_("'{0}' is not a field this form's grain may write to '{1}' (field '{2}'). Allowed: {3}.").format(
				field, table, m.source_field or "?", allowed
			),
			title=_("Target Not In This Grain"),
		)

	def _validate_phone_mapping(self):
		"""Fail-closed: the lead is keyed and deduped on phone, so an ENABLED form with fields
		MUST have exactly one field mapped to lead -> mobile_no (the operator declares WHICH field
		is the phone — nothing is hardcoded). A draft (disabled) or empty form may be saved while
		still being built; it just can't go live without a phone."""
		if not self.enabled or not self.mappings:
			return
		phone = layers.phone_of(self)
		phone_maps = [m for m in self.mappings if layers.target_pair(m) == phone]
		layer = layers.layer_of(self)
		if layer:
			# The person is found by phone or email, so a layer form needs either — and at most one phone.
			if len(phone_maps) > 1:
				frappe.throw(_("At most one field may map to {0} → {1} (the phone) — found {2}.").format(
					phone[0], phone[1], len(phone_maps)), title=_("Phone Mapping Required"))
			if not phone_maps and not any(layers.target_pair(m) == layer.email for m in self.mappings):
				frappe.throw(_("Map a question to {0} → {1} or {0} → {2}, so the person can be found.").format(
					layer.phone[0], layer.phone[1], layer.email[1]), title=_("Phone or Email Required"))
			return
		if len(phone_maps) == 1:
			return
		frappe.throw(
			_(
				"Exactly one field must map to lead → Mobile No (the patient's phone) — found {0}. "
				"The lead is deduped on phone, so an enabled form needs one."
			).format(len(phone_maps)),
			title=_("Phone Mapping Required"),
		)
