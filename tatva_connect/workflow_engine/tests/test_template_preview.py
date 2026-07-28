# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W3 — the author sees what would really go out, before a patient does.

`preview` is not a new mechanism: it is the declaration Call API already carries — a whitelisted method
plus the sibling args to call it with — put on the two send nodes' `template_values`. No preview endpoint
per node, no new control contract; the only new code is the render itself.

AND THE RENDER MUST NOT LIE, which is why there are TWO of them. An Email Template is rendered by
`frappe.render_template` over its own subject and body, which is literally what `send_email` calls — so
the email preview IS the message. A WhatsApp body is assembled PROVIDER-SIDE from the template plus the
parameters, so rendering it here would be our reproduction of someone else's renderer; that preview
returns the stored body and the parameter list `_template_parameters` really built, and nothing invented.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions, sends
from tatva_connect.workflow_engine import registry

_NODES = ("Send WhatsApp", "Send Email")


def _param(node_type, name):
	return next(p for p in actions.VERBS[node_type]["params"] if p["name"] == name)


def _config_field(node_type, name):
	return next(f for f in registry.declaration(node_type)["config"] if f["name"] == name)


class TestThePreviewIsDeclared(FrappeTestCase):
	def test_both_send_nodes_declare_one_on_their_value_map(self):
		for node_type in _NODES:
			with self.subTest(node=node_type):
				preview = _param(node_type, "template_values").get("preview")

				self.assertTrue(preview, f"{node_type} declares no preview")
				self.assertTrue(preview["args"], "a preview with no args cannot be built from the node")

	def test_its_args_name_real_siblings_of_the_same_node(self):
		"""An arg naming a field the node does not have resolves to '' and previews the wrong thing."""
		for node_type in _NODES:
			with self.subTest(node=node_type):
				siblings = {p["name"] for p in actions.VERBS[node_type]["params"]}
				named = set(_param(node_type, "template_values")["preview"]["args"].values())

				self.assertLessEqual(named, siblings)

	def test_the_method_is_whitelisted(self):
		"""Declared but not whitelisted is a preview button that 403s — green here, dead on the canvas."""
		for node_type in _NODES:
			with self.subTest(node=node_type):
				method = _param(node_type, "template_values")["preview"]["method"]

				self.assertIn(frappe.get_attr(method), frappe.whitelisted, f"{method} is not whitelisted")

	def test_it_reaches_the_canvas(self):
		"""`_verb_field` carries everything a verb declares; this is the assertion that it still does."""
		for node_type in _NODES:
			with self.subTest(node=node_type):
				self.assertTrue(_config_field(node_type, "template_values").get("preview"))


class TestTheEmailPreviewIsTheMessage(FrappeTestCase):
	def setUp(self):
		self.template = frappe.get_doc({
			"doctype": "Email Template",
			"name": f"WF-PREVIEW-{frappe.generate_hash(length=6)}",
			"subject": "Hello {{ patient }}",
			"use_html": 0,
			"response": "Your programme is {{ programme }}.",
		}).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("Email Template", self.template.name, force=True))

	def tearDown(self):
		frappe.db.rollback()

	def _rows(self, **literals):
		from tatva_connect.workflow_engine import contract

		return [{"name": k, "mode": contract.LITERAL, "value": v} for k, v in literals.items()]

	def test_it_renders_the_declared_values_into_subject_and_body(self):
		answer = sends.email_template_preview(
			self.template.name, self._rows(patient="Asha", programme="Cardiac")
		)

		self.assertEqual(answer["subject"], "Hello Asha")
		self.assertIn("Cardiac", answer["body"])
		self.assertEqual(answer["blank"], [])

	def test_a_slot_resolving_to_nothing_is_REPORTED_never_rendered_away(self):
		"""The one thing this whole layer exists to prevent: a hole a patient would read as normal text."""
		answer = sends.email_template_preview(
			self.template.name, self._rows(patient="Asha", programme="")
		)

		self.assertEqual(answer["blank"], ["programme"])
		self.assertNotIn("Asha", answer["subject"], "nothing is rendered while a slot is blank")

	def test_an_unmapped_slot_answers_instead_of_500ing_the_panel(self):
		"""Mid-mapping is the author's ordinary state; the filler raises and the preview must not."""
		answer = sends.email_template_preview(self.template.name, self._rows(patient="Asha"))

		self.assertTrue(answer["error"])

	def test_the_wire_form_of_values_is_accepted(self):
		"""The browser sends JSON text, a test sends a list — parsed once, in the shared gate."""
		answer = sends.email_template_preview(
			self.template.name, frappe.as_json(self._rows(patient="Asha", programme="Cardiac"))
		)

		self.assertEqual(answer["subject"], "Hello Asha")
