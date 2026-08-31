# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Assign to User is grained — at execution, and in the picker that offers the user.

`assign_to_user` is a plain `Link` to `User`. `registry._scoped` only marks a link grain-scoped when the
TARGET DOCTYPE carries all three axes, and `User` carries none and never will — so the picker offered every
user on the site and execution asserted nothing. A workflow scoped to Goodflip-Care/Anaya could hand a lead
to a rep entitled only to Tatvapractice, and no layer would say a word.

TWO HALVES, ONE BRAIN
---------------------
Both halves ask `access.entitlement` — the same brain that decides which leads and fields a rep may see.
There is no second notion of "may this user act on this grain", and no query against the permission
tables: a reverse query would be a second matcher that could disagree with the forward one.

They ask DIFFERENT questions of it, and the difference is the grain rule (B9):

  * EXECUTION has a real lead, so it asks about a DATA grain — `entitlement.grain_entitled`.
  * THE PICKER has only the workflow's DECLARED grain, which is a RULE grain whose blank axis means ANY.
    It therefore asks the possibility question — `entitlement.grain_overlaps_entitlement` — because
    handing a rule grain to the data-grain resolver compares a wildcard as the literal empty string and
    answers confidently wrong in both directions. That is the same split as `is_settable` vs
    `is_set_declared`, for the same reason.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import entitlement
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.workflow_engine import interpreter, refs, registry
from tatva_connect.workflow_engine.tests import fixtures as fx

_ENTITLED = "wf-assign-entitled@example.invalid"
_FOREIGN = "wf-assign-foreign@example.invalid"
_FOREIGN_GRAIN = GRAINS[0]  # Goodflip-Care / Anaya / Nivolumab — a different vertical AND group
_RULE_PREFIX = "WF Assign Grain"
_GRAINS_CACHE = "tatva_connect:entitled_grains"


def _make_user(email):
	if frappe.db.exists("User", email):
		return email
	frappe.get_doc({
		"doctype": "User", "email": email, "first_name": "WF Assign", "send_welcome_email": 0,
		"user_type": "System User", "enabled": 1,
	}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
	return email


def _make_rule(name, user, grain):
	"""A live CRM Lead Assignment Rule at one grain — the SOURCE `entitlement._internal_grains` reads.

	Not a stub: entitlement derives an internal principal's grains from real Assignment Rule rows, so a
	test that mocked that away would prove nothing about the brain it claims to use.
	"""
	if frappe.db.exists("Assignment Rule", name):
		return name
	frappe.get_doc({
		"doctype": "Assignment Rule", "name": name, "document_type": "CRM Lead",
		"description": "workflow assign-grain probe", "assign_condition": "status == 'New'",
		"rule": "Round Robin", "disabled": 0,
		"grain_vertical": grain["vertical"], "grain_group": grain["group"], "grain_program": grain["program"],
		"users": [{"user": user}],
		"assignment_days": [{"day": "Monday"}],
	}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
	return name


def _forget_grains():
	"""Entitlement is request-cached; a test that creates a rule must drop the memo it invalidates."""
	setattr(frappe.local, _GRAINS_CACHE, None)
	setattr(frappe.local, entitlement._REGISTRY_FLAG_CACHE, None)


def _set_registry(enabled):
	"""Arm or disarm the switch that CHOOSES which entitlement source is read, and drop its memo."""
	frappe.db.set_value("CRM Tatva Automation", entitlement.REGISTRY_FLAG, "enabled",
	                    1 if enabled else 0, update_modified=False)
	setattr(frappe.local, entitlement._REGISTRY_FLAG_CACHE, None)


class TestAssignGrain(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		# This suite grants entitlement through Assignment Rules, which is what `entitled_grains` reads
		# while `Access::Grain::registry` is dormant; armed, it reads native User Permission instead and
		# never reaches the roll-up, so every rule seeded here would grant nothing. The switch is pinned
		# to the path being exercised and restored afterwards — a bench that has it armed is testing the
		# other mechanism, not a broken one.
		cls._registry_was = frappe.db.get_value("CRM Tatva Automation", entitlement.REGISTRY_FLAG, "enabled")
		_set_registry(0)
		cls.entitled = _make_user(_ENTITLED)
		cls.foreign = _make_user(_FOREIGN)
		_make_rule(f"{_RULE_PREFIX} Home", cls.entitled, fx.GRAIN)
		_make_rule(f"{_RULE_PREFIX} Away", cls.foreign, _FOREIGN_GRAIN)
		cls.lead = fx.make_lead()
		frappe.db.commit()
		_forget_grains()

	@classmethod
	def tearDownClass(cls):
		"""Nothing this suite armed may outlive it — a rule or a user left behind is a live config change."""
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		for suffix in ("Home", "Away"):
			if frappe.db.exists("Assignment Rule", f"{_RULE_PREFIX} {suffix}"):
				frappe.delete_doc("Assignment Rule", f"{_RULE_PREFIX} {suffix}", force=True, ignore_permissions=True)
		for email in (_ENTITLED, _FOREIGN):
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		_set_registry(cls._registry_was)
		frappe.db.commit()
		_forget_grains()

	def _assign(self, user):
		"""Run the verb the way the interpreter runs it, on a lead whose grain is `fx.AXES`."""
		node = frappe._dict({
			"node_id": "a", "node_type": "Assign to User", "edges": [],
			"config_json": frappe.as_json({
				"assign_mode": "Assign", "assignee_mode": "User", "assign_to_user": user,
			}),
		})
		state = refs.Values()
		interpreter._run_verb(node, self.lead.name, self.lead, state, fx.AXES)
		return state

	# --- execution ---------------------------------------------------------------------------------------

	def test_a_rep_entitled_to_the_leads_grain_is_assigned(self):
		"""The other direction first: a gate that refused everyone would pass the refusal test below."""
		_forget_grains()
		self.assertEqual(self._assign(self.entitled).get("a.assigned_to"), self.entitled)

	def test_a_rep_entitled_to_another_grain_is_refused(self):
		"""The defect. A Goodflip-Care rep could be handed a Tatvapractice lead and nothing objected."""
		_forget_grains()
		with self.assertRaises(PermissionError):
			self._assign(self.foreign)

	def test_a_run_with_no_resolved_grain_still_assigns(self):
		"""A non-Lead subject on the durable path carries no axes. There is no grain to enforce, so the
		check does not invent one — refusing here would break every File-triggered workflow."""
		_forget_grains()
		node = frappe._dict({
			"node_id": "a", "node_type": "Assign to User", "edges": [],
			"config_json": frappe.as_json({
				"assign_mode": "Assign", "assignee_mode": "User", "assign_to_user": self.foreign,
			}),
		})
		state = refs.Values()
		interpreter._run_verb(node, self.lead.name, self.lead, state, (None, None, None))
		self.assertEqual(state.get("a.assigned_to"), self.foreign)

	# --- the picker --------------------------------------------------------------------------------------

	def test_the_picker_offers_only_users_the_workflows_grain_entitles(self):
		_forget_grains()
		offered = entitlement.users_entitled_to(fx.AXES, limit=500, scan=5000)
		self.assertIn(self.entitled, offered)
		self.assertNotIn(self.foreign, offered)

	def test_a_blank_axis_on_the_workflow_grain_means_ANY_and_widens_the_picker(self):
		"""B9. The workflow's grain is a RULE grain: `program=""` means every program, so a rep entitled to
		one specific program under that vertical/group is eligible. Asked as a DATA grain — `("TP","India","")`
		compared literally — this rep disappears, which is the exact defect class."""
		_forget_grains()
		offered = entitlement.users_entitled_to((fx.GRAIN["vertical"], fx.GRAIN["group"], ""), limit=500, scan=5000)
		self.assertIn(self.entitled, offered)
		self.assertNotIn(self.foreign, offered)

	def test_a_wholly_blank_workflow_grain_offers_both(self):
		"""Every axis blank means the workflow applies everywhere, so it may assign to anyone."""
		_forget_grains()
		offered = entitlement.users_entitled_to(("", "", ""), limit=500, scan=5000)
		self.assertIn(self.entitled, offered)
		self.assertIn(self.foreign, offered)

	def test_the_assignee_control_declares_how_it_is_scoped(self):
		"""The seam, locked. `User` cannot carry axes, so the scoping is DECLARED and resolved in one
		place — not bolted on as a special case for one doctype."""
		param = next(f for f in registry.config_fields("Assign to User") if f["name"] == "assign_to_user")
		self.assertEqual(param.get("scope"), registry.ENTITLED_USERS)

		emitted = next(t for t in registry.node_types() if t["type"] == "Assign to User")
		control = next(f for f in emitted["config"] if f["name"] == "assign_to_user")
		self.assertTrue(control["grain_scoped"], "the assignee picker is still unscoped")
		self.assertEqual(control["scope_kind"], registry.ENTITLED_USERS)

	def test_every_declared_scoping_kind_has_exactly_one_resolver(self):
		"""A kind a field may declare but nothing can resolve would leave the picker silently unscoped."""
		for node_type in registry.NODE_TYPES:
			for field in registry.config_fields(node_type):
				kind = field.get("scope")
				if not kind:
					continue
				with self.subTest(node_type=node_type, field=field["name"]):
					self.assertIn(kind, registry.SCOPE_KINDS, f"{kind} is declared but has no resolver")
