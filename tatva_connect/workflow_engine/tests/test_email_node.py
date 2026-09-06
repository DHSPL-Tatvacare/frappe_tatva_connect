# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE EMAIL NODE IS THE STRUCTURAL TWIN OF SEND WHATSAPP. NO FREE COMPOSE, NO TYPED RECIPIENT.

`Send Email` was the last free-form outbound surface: a typed `Data` subject, a `Small Text` compose box,
and a recipient carrying `free_text: True` so an author could type an address straight onto the node.

The canvas sends to people whose details are known in advance, and every message goes through the org's
template chain. So the node asks the same three questions Send WhatsApp asks:

  who     `email_recipient`  Variable, reqd, PICKED from the grouped picker — never typed
  what    `email_template`   Link to Frappe's own `Email Template`
  values  `template_values`  the same Value Map control, mapping upstream values to the template's slots

`free_text` is deleted, not merely unset. While the mechanism existed a future author could re-open the
typed-literal hole: `resolve_recipient` treated a phone-shaped string as an address to send to, which is
the same class of hole as the typed number that reached the wrong subscriber.

THE ONE REAL DIFFERENCE from WhatsApp is the slot source, and it is a TWIN rather than a special case:
WhatsApp templates carry positional `{{1}}` slots the provider declares, while an `Email Template` carries
NAMED Jinja variables in its subject and body. Two slot readers, one mapping control, no if-branch.
"""
import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions, sends
from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import graph, refs, registry
from tatva_connect.workflow_engine.tests import fixtures as fx

_TEMPLATE = "W2 Email Probe Template"
_VERB = "Send Email"


def _params():
	return {p["name"]: p for p in actions.VERBS[_VERB]["params"]}


class TestTheEmailNodeDeclaresTheSameThreeThings(FrappeTestCase):
	"""The declaration, asserted rather than described."""

	def test_the_recipient_is_picked_never_typed(self):
		"""THE red. `free_text: True` is what let an author type an address onto the node."""
		field = _params()["email_recipient"]
		self.assertEqual(field["type"], "Variable")
		self.assertTrue(field["reqd"])
		self.assertFalse(field.get("free_text"), "an address must be picked, never typed")

	def test_it_links_a_frappe_email_template(self):
		field = _params()["email_template"]
		self.assertEqual(field["type"], "Link")
		self.assertEqual(field["link"], "Email Template", "Frappe already has a template store")
		self.assertTrue(field["reqd"])

	def test_it_maps_values_through_the_same_control_whatsapp_uses(self):
		field = _params()["template_values"]
		self.assertEqual(field["type"], "Value Map")
		self.assertEqual(registry.read_kind_of(field), "value_rows", "implied by the type, not restated")
		self.assertEqual(field["slots_from"], "email_template")

	def test_the_free_compose_surface_is_gone(self):
		"""Deleted, not hidden: a subject box and a body box are what the template chain replaces."""
		params = _params()
		self.assertNotIn("email_subject", params)
		self.assertNotIn("email_body", params)

	def test_it_is_structurally_the_twin_of_send_whatsapp(self):
		"""The point of the chunk, stated as a shape: both nodes ask who / what / values, and neither
		offers a free-form anything."""
		email = {p["name"]: p["type"] for p in actions.VERBS[_VERB]["params"]}
		whatsapp = {p["name"]: p["type"] for p in actions.VERBS["Send WhatsApp"]["params"]}

		self.assertEqual(email["email_recipient"], whatsapp["contact_number"])
		self.assertEqual(email["template_values"], whatsapp["template_values"])
		self.assertEqual(
			sorted(t for t in email.values()), sorted(["Variable", "Link", "Value Map"]),
		)


class TestTheFreeTextMechanismIsDeleted(FrappeTestCase):
	"""G4/G5 — the last declaration using it is gone, so the mechanism goes with it. A path nothing
	declares but everything still supports is the second answer this effort exists to remove."""

	def test_no_declaration_anywhere_carries_free_text(self):
		offenders = [
			f"{verb}.{p['name']}"
			for verb, declared in actions.VERBS.items()
			for p in declared.get("params") or []
			if p.get("free_text")
		]
		self.assertEqual(offenders, [], f"{offenders} can still be typed by hand")

	def test_the_literal_resolver_is_gone(self):
		"""`resolve_recipient` returned a phone-shaped literal AS the address to send to."""
		self.assertFalse(hasattr(sends, "resolve_recipient"), "the typed-literal path must be deleted")


class TestTheSlotsComeFromTheTemplate(FrappeTestCase):
	"""The email twin of `template_slots`. Named Jinja variables, read from subject AND body."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		if frappe.db.exists("Email Template", _TEMPLATE):
			frappe.delete_doc("Email Template", _TEMPLATE, force=True, ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Email Template", "name": _TEMPLATE, "enabled": 1,
			"subject": "Your visit with {{ doctor_name }}",
			"use_html": 0,
			"response": "<p>Hello {{ patient_name }}, please arrive by {{ visit_time }}.</p>",
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, through the document API (B11)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("Email Template", _TEMPLATE, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def test_every_named_slot_in_subject_and_body_is_offered(self):
		"""THE red — there was no email slot reader at all, so the mapping control had nothing to offer."""
		self.assertEqual(
			sorted(sends.email_template_slots(_TEMPLATE)),
			["doctor_name", "patient_name", "visit_time"],
		)

	def test_a_template_with_no_slots_offers_none(self):
		"""The other direction: a static template must not invent rows for the author to fill."""
		name = f"{_TEMPLATE} Static"
		frappe.get_doc({
			"doctype": "Email Template", "name": name, "enabled": 1,
			"subject": "Your appointment", "use_html": 0, "response": "<p>See you soon.</p>",
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, through the document API (B11)
		self.addCleanup(frappe.delete_doc, "Email Template", name, force=True, ignore_permissions=True)

		self.assertEqual(sends.email_template_slots(name), [])

	def test_the_reader_is_gated_like_its_whatsapp_twin(self):
		"""It reads operator content through a whitelisted endpoint, so it asks the same permission."""
		with patch.object(frappe, "has_permission", return_value=False):
			with self.assertRaises(frappe.PermissionError):
				sends.email_template_slots(_TEMPLATE)

	def test_the_endpoint_the_control_fetches_is_really_whitelisted(self):
		"""The half the test above assumed. `actions.VERBS` declares this path as the Value Map's
		`slots_method` and `ValueMap` calls it as a url, so without the decorator the control fetched
		nothing, the author had no rows to map, and publish then refused the slots they never saw."""
		self.assertIn(sends.email_template_slots, frappe.whitelisted)

	def test_the_core_the_send_reads_asks_no_author_permission(self):
		"""Its twin `whatsapp_template_slots` asks none either, and for the same reason — see the send test."""
		with patch.object(frappe, "has_permission", return_value=False):
			self.assertEqual(sorted(sends._email_slots(_TEMPLATE)),
			                 ["doctor_name", "patient_name", "visit_time"])


class TestTheEmailSendResolvesAndRenders(FrappeTestCase):
	"""Runtime, driven through the real verb handler. Nothing is sent: the sends switch ships OFF and the
	mail boundary is captured in-process."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		if frappe.db.exists("Email Template", _TEMPLATE):
			frappe.delete_doc("Email Template", _TEMPLATE, force=True, ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Email Template", "name": _TEMPLATE, "enabled": 1,
			"subject": "Your visit with {{ doctor_name }}", "use_html": 0,
			"response": "<p>Hello {{ patient_name }}.</p>",
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, through the document API (B11)
		cls.lead = fx.make_lead()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Email Template", _TEMPLATE, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def _send(self, recipient_ref, context, values):
		params = frappe._dict({
			"action_type": _VERB, "email_recipient": recipient_ref,
			"email_template": _TEMPLATE, "template_values": values,
		})
		seen = {}
		ctx = dict(context)
		with patch.object(sends, "sends_enabled", return_value=True), \
		     patch.object(frappe, "sendmail", lambda **kw: seen.update(kw)):
			actions._action_send_email(params, self.lead.name, ctx, None, None)
		return ctx.get(refs.OUTPUT), seen

	def test_the_declared_recipient_is_the_address_and_the_template_is_rendered(self):
		"""THE headline: who comes from the picked reference, what comes from the template, and the slots
		are filled from the declared mapping."""
		output, seen = self._send(
			"sv.email",
			{"sv.email": "asha@example.invalid", "sv.doctor": "Dr Rao", "sv.patient": "Asha"},
			[
				{"name": "doctor_name", "mode": "From Context", "value": "sv.doctor"},
				{"name": "patient_name", "mode": "From Context", "value": "sv.patient"},
			],
		)

		self.assertEqual(output, sends.SENT)
		self.assertEqual(seen.get("recipients"), ["asha@example.invalid"])
		self.assertEqual(seen.get("subject"), "Your visit with Dr Rao")
		self.assertIn("Hello Asha", seen.get("message"))

	def test_a_recipient_that_resolves_to_nothing_routes_to_failed(self):
		"""DATA, not an exception — the journey must not die because one record has no address."""
		output, seen = self._send("sv.email", {}, [])

		self.assertEqual(output, sends.FAILED)
		self.assertEqual(seen, {}, "nothing may reach the mail boundary")

	def test_a_typed_address_is_not_treated_as_an_address(self):
		"""The deleted hole, asserted. An author who somehow stores a literal must NOT have it mailed:
		it is a reference that resolves to nothing, exactly as a misspelt one would."""
		output, seen = self._send("ops@tatvacare.invalid", {}, [])

		self.assertEqual(output, sends.FAILED)
		self.assertEqual(seen, {}, "a typed literal must never become the address")

	def test_a_slot_the_author_left_unmapped_is_author_error(self):
		"""Same rule as WhatsApp: a missing row is wrong for every record equally, so it raises rather
		than sending a mail with a blank in it."""
		with self.assertRaises(ValueError):
			self._send("sv.email", {"sv.email": "a@b.invalid"}, [])

	def test_publish_refuses_a_recipient_nothing_upstream_produces(self):
		"""Declaring it is what turns a misspelt address into an authoring error."""
		nodes = [
			{"node_id": "start", "node_type": "Trigger",
			 "config_json": json.dumps({"subject_doctype": "CRM Lead", "event": "Created"}),
			 "edges": [{"from_output": "next", "to_node": "em"}]},
			{"node_id": "em", "node_type": _VERB,
			 "config_json": json.dumps({"email_recipient": "nothing_makes_this", "email_template": _TEMPLATE}),
			 "edges": [{"from_output": "sent", "to_node": "end"}, {"from_output": "failed", "to_node": "end"}]},
			{"node_id": "end", "node_type": "Terminal", "config_json": "{}", "edges": []},
		]
		messages = " | ".join(p["message"] for p in graph.problems(nodes, entry_node="start"))
		self.assertIn("nothing_makes_this", messages)


class TestTheSendDoesNotAskTheAuthorsPermission(FrappeTestCase):
	"""A journey runs as whoever saved the record, and that person is a rep.

	`frappe.enqueue` carries `frappe.session.user` onto the job and `execute_job` sets it, so the durable
	lane is the rep too — not Administrator. A rep holds no `CRM Workflow` read, so while the send path
	called the author-facing reader every rep-triggered Send Email raised `PermissionError` at the node.
	It raised BEFORE the dormant gate as well, so a bench with sends switched off raised too — the one
	thing `DORMANT_MARKER` exists to promise cannot happen.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		if frappe.db.exists("Email Template", _TEMPLATE):
			frappe.delete_doc("Email Template", _TEMPLATE, force=True, ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Email Template", "name": _TEMPLATE, "enabled": 1,
			"subject": "Your visit with {{ doctor_name }}", "use_html": 0,
			"response": "<p>Hello {{ patient_name }}.</p>",
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, through the document API (B11)
		cls.lead = fx.make_lead()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Email Template", _TEMPLATE, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def _send_as_unprivileged(self, live):
		params = frappe._dict({
			"action_type": _VERB, "email_recipient": "sv.email",
			"email_template": _TEMPLATE, "template_values": [
				{"name": "doctor_name", "mode": "From Context", "value": "sv.doctor"},
				{"name": "patient_name", "mode": "From Context", "value": "sv.patient"},
			],
		})
		ctx = {"sv.email": "asha@example.invalid", "sv.doctor": "Dr Rao", "sv.patient": "Asha"}
		seen = {}
		with patch.object(frappe, "has_permission", return_value=False), \
		     patch.object(sends, "sends_enabled", return_value=live), \
		     patch.object(frappe, "sendmail", lambda **kw: seen.update(kw)):
			marker = actions._action_send_email(params, self.lead.name, ctx, None, None)
		return ctx.get(refs.OUTPUT), marker, seen

	def test_a_live_send_reaches_the_patient(self):
		output, _marker, seen = self._send_as_unprivileged(live=True)
		self.assertEqual(output, sends.SENT)
		self.assertEqual(seen.get("recipients"), ["asha@example.invalid"])
		self.assertEqual(seen.get("subject"), "Your visit with Dr Rao")

	def test_a_dormant_send_is_suppressed_rather_than_raised(self):
		"""Dormant suppresses the message, never the shape of the graph — the module header's own promise."""
		output, marker, seen = self._send_as_unprivileged(live=False)
		self.assertEqual(output, sends.SENT)
		self.assertTrue(sends.was_suppressed(marker))
		self.assertEqual(seen, {}, "a dormant bench must send nothing")
