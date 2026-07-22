# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""ONE vocabulary, in BOTH places a predicate is judged.

A predicate control is one control, and an author who builds `Status is New` means one thing by it. But
the engine judges predicates in two entirely different places, against two contexts built by two
different code paths:

  * a TRIGGER predicate is judged at DISPATCH, against `automation.context.context_for(doc, changed)` —
    the doc's own field dict;
  * a BRANCH predicate is judged at EXECUTION, against the run's state.

Nothing joined them. Namespace one and not the other and the same control means two different things,
which is precisely the Trigger/Branch divergence this codebase has already found once.

The concrete failure this suite locks: `Call API` emits `status`, and `CRM Lead` has a `status` column.
Under a flat vocabulary those are ONE name. Downstream of a Call API the author is offered a single
`Status` — the HTTP one — the lead's own status is unpickable, and the identical predicate that matched
at the Trigger cannot match at the Branch. Nothing anywhere says so.

Under the namespaced contract they are `crm_lead.status` and `api.status`: two values, two labels, and
the predicate an author built at the Trigger resolves identically at a Branch.
"""
import json

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import context as trigger_context
from tatva_connect.automation import expr, rules
from tatva_connect.workflow_engine import interpreter, refs, upstream
from tatva_connect.workflow_engine.tests import fixtures as fx

_CAPTURING_NODE = "api"


def _node(node_id, node_type, config=None, edges=None):
	return {
		"node_id": node_id,
		"node_type": node_type,
		"config_json": json.dumps(config or {}),
		"edges": [{"from_output": o, "to_node": t} for o, t in (edges or {}).items()],
	}


def _graph():
	"""Trigger → Call API → Branch → End. The Call API is what makes `status` ambiguous."""
	return [
		_node("start", "Trigger", {"subject_doctype": "CRM Lead", "event": "Created"}, {"next": _CAPTURING_NODE}),
		_node(_CAPTURING_NODE, "Call API", {"webhook_endpoint": "x"}, {"succeeded": "b1", "failed": "b1"}),
		_node("b1", "Branch", {}, {"true": "end", "false": "end"}),
		_node("end", "Terminal"),
	]


def _node_writes(state, node_id, key, value):
	"""Model "node `node_id` wrote `key`" against whatever shape run state has.

	The engine scopes a writer's values under that writer; a state object that cannot express a writer
	has one flat bag and the write goes there. Either way this is the same event — a Call API capturing
	its HTTP status — so the assertions below test meaning, never storage.
	"""
	getattr(state, "writing_as", lambda _n: state)(node_id)[key] = value


class TestContextCoherence(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		cls.lead = fx.make_lead()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _offered_at_branch(self):
		return upstream.available_at(_graph(), "b1")

	def _lead_status_ref(self):
		"""The reference the picker offers for the LEAD's own Status, at a Branch below the Call API."""
		offered = [v for v in self._offered_at_branch() if v["source"] != _CAPTURING_NODE and v["label"] == "Status"]
		self.assertTrue(
			offered,
			"the lead's own Status is not offered below a Call API — the node's `status` has eaten it",
		)
		return offered[0]["ref"]

	def _node_status_ref(self):
		offered = [v for v in self._offered_at_branch() if v["source"] == _CAPTURING_NODE and v["ref"].endswith("status")]
		self.assertTrue(offered, "the Call API's own HTTP status must still be offered")
		return offered[0]["ref"]

	# --- the vocabulary -----------------------------------------------------------------------------

	def test_the_lead_and_the_node_offer_two_distinct_references(self):
		"""The structural half. Two different values may not share one name."""
		self.assertNotEqual(
			self._lead_status_ref(), self._node_status_ref(),
			"a node's value and a record column resolved to the same name — one of them is unreachable",
		)

	def test_every_reference_says_where_it_came_from(self):
		"""Collision-free by construction, not by de-duplication: every key is `<source>.<field>`."""
		for value in self._offered_at_branch():
			self.assertIn(".", value["ref"], f"{value['ref']} does not say where it came from")

	# --- the coherence: ONE predicate, both places ---------------------------------------------------

	def test_the_same_predicate_resolves_identically_at_the_trigger_and_at_a_branch(self):
		"""The headline. The author builds it once; it must mean the same thing in both evaluators."""
		ref = self._lead_status_ref()
		predicate = {"type": "rule", "field": ref, "operator": "is", "value": "New"}

		at_trigger = trigger_context.context_for(frappe.get_doc("CRM Lead", self.lead.name), {})
		self.assertTrue(
			rules.predicate_match(predicate, at_trigger),
			f"{ref} does not answer in the TRIGGER context — the two contexts speak different languages",
		)

		at_branch = interpreter._refreshed_state(frappe._dict(
			subject_doctype="CRM Lead", subject_name=self.lead.name, state_json="{}",
		))
		_node_writes(at_branch, _CAPTURING_NODE, "status", 500)
		self.assertTrue(
			rules.predicate_match(predicate, at_branch),
			f"{ref} resolved to the Call API's HTTP status at the Branch — the same control, two meanings",
		)

	def test_the_node_reference_reads_the_node_and_not_the_record(self):
		"""The mirror image, so the fix cannot be "make everything mean the record"."""
		at_branch = interpreter._refreshed_state(frappe._dict(
			subject_doctype="CRM Lead", subject_name=self.lead.name, state_json="{}",
		))
		_node_writes(at_branch, _CAPTURING_NODE, "status", 500)
		predicate = {"type": "rule", "field": self._node_status_ref(), "operator": "is", "value": 500}
		self.assertTrue(rules.predicate_match(predicate, at_branch))

	# --- author expressions: the context must be a real mapping, not a dict ---------------------------

	def test_an_author_expression_resolves_a_namespaced_reference(self):
		"""`ctx["crm_lead.status"]` is an ordinary string subscript, so it parses and `context_keys` reads
		it. What it needs is an object that can ANSWER it."""
		state = interpreter._refreshed_state(frappe._dict(
			subject_doctype="CRM Lead", subject_name=self.lead.name, state_json="{}",
		))
		self.assertEqual(
			expr.resolve_expression('ctx["crm_lead.status"]', state),
			frappe.db.get_value("CRM Lead", self.lead.name, "status"),
		)

	def test_the_expression_context_is_backed_by_the_resolver_not_a_copied_dict(self):
		"""The half a flat dict CANNOT do, which is why `Values` is a real mapping.

		The subject is never copied into run state — it is served off the live document on first reference.
		So a dict of namespaced keys built at the top of the segment would answer `crm_lead.*` with whatever
		was true then, and `safe_eval` would happily compute on a stale value. Proven by changing the
		document after the state object exists: the expression must see the NEW value.
		"""
		state = interpreter._refreshed_state(frappe._dict(
			subject_doctype="CRM Lead", subject_name=self.lead.name, state_json="{}",
		))
		frappe.db.set_value("CRM Lead", self.lead.name, "first_name", "Resolved-Late")
		frappe.db.commit()
		self.assertEqual(expr.resolve_expression('ctx["crm_lead.first_name"]', state), "Resolved-Late")

	def test_context_keys_extracts_a_namespaced_reference(self):
		"""The publish gate reads what an expression references through this. A namespaced key must survive
		it, or every Expression-mode field would look like it references nothing and be checked by nothing."""
		self.assertEqual(
			expr.context_keys('add_days(ctx["crm_lead.custom_dob"], 3)'), {"crm_lead.custom_dob"}
		)

	# --- the `__before` pair: the suffix goes on the FIELD, never on the source ------------------------

	def _changed_context(self):
		"""A Trigger context for a lead whose `status` moved New → Qualified, built the ONE way."""
		doc = frappe.get_doc("CRM Lead", self.lead.name)
		doc.status = "Qualified"
		return trigger_context.context_for(doc, {"status": ("New", "Qualified")})

	def test_the_before_value_is_namespaced_on_the_field_not_on_the_source(self):
		"""The FORM, locked. `crm_lead.status__before` — one source, one longer field name.

		The alternative (`crm_lead__before.status`) would invent a SOURCE no writer ever writes as, and
		`refs.parse` splits once from the left, so it would resolve against a record that does not exist.
		This form also keeps `rules._changed_match` composing `f"{field}__before"` with no knowledge of the
		namespace at all — the same line that worked on bare names works on namespaced ones.
		"""
		context = self._changed_context()
		self.assertIn("crm_lead.status__before", context)
		self.assertEqual(context["crm_lead.status__before"], "New")
		self.assertNotIn("crm_lead__before.status", context, "the suffix belongs to the field, not the source")

	def test_changed_to_reads_the_namespaced_before_value(self):
		"""Operator one of two. It must also require that the value really MOVED."""
		context = self._changed_context()
		ref = refs.of_record("CRM Lead", "status")
		self.assertTrue(rules.predicate_match(
			{"type": "rule", "field": ref, "operator": "changed to", "value": "Qualified"}, context,
		))
		self.assertFalse(rules.predicate_match(
			{"type": "rule", "field": ref, "operator": "changed to", "value": "New"}, context,
		), "it moved to Qualified, not to New")

	def test_changed_from_to_reads_the_namespaced_before_value(self):
		"""Operator two of two — the one that reads BOTH ends of the pair."""
		context = self._changed_context()
		ref = refs.of_record("CRM Lead", "status")
		self.assertTrue(rules.predicate_match({
			"type": "rule", "field": ref, "operator": "changed from…to",
			"from_value": "New", "value": "Qualified",
		}, context))
		self.assertFalse(rules.predicate_match({
			"type": "rule", "field": ref, "operator": "changed from…to",
			"from_value": "Lost", "value": "Qualified",
		}, context), "it did not come from Lost")
