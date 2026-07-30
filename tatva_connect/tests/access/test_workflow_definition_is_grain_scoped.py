# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A workflow is scoped by the line IT declares — not by a parent lead, and not by who authored it.

`CRM Workflow` was the one member of the workflow family nobody scoped. Its three children were, so a rep
could not read a journey's saved state — but could open the Workflows list and read every business line's
rules: which fields they test, which templates they send, which programmes they run.

IT IS SCOPED BY A DECLARED STRATEGY, NOT BY A FUNCTION OF ITS OWN. `visibility.SCOPED["CRM Workflow"]`
says `[RuleGrain("trigger_vertical", "trigger_group", "trigger_program")]` and that is the entire
difference from a Task or a Note, which say `[Own(...), ViaParent(...)]`. The switch gate, the privilege
short-circuit, fail-open-when-off and fail-closed-to-`1=0` are the same lines of code for all of them.

That matters here because the first cut of this work did the opposite: a parallel `grain_scoped_pqc`,
`grain_admits`, `grain_readable_names` and `grain_scoped_has_permission` beside the existing family, each
re-stating the switch, privilege, fail-open and `1=0` rules — while `smartview/permissions.py` held a
third copy of the same wildcard-grain rule. Three implementations of one question. The tests below are
written against the shared entry points precisely so they cannot be satisfied by a fourth.

`Own` is deliberately NOT among a workflow's strategies: it would show a rep only the workflows they
personally authored, which is not what "may see" means for a rule that governs their own patients.

THE GRAIN IS A RULE GRAIN. A workflow may leave an axis blank, and blank means ANY — so the question is
`entitlement.grain_overlaps_entitlement`, never `grain_entitled`, which takes a record's DATA grain and
would read that blank as the literal empty string. That is the defect that once hid 129 fields from
1,894 leads with every test green, and it is why none of the assertions below compare a grain tuple.

WHAT THIS DELIBERATELY DOES NOT CHANGE — asserted at the bottom, because it is the expensive mistake
available here. Scoping the LIST does not change which leads a workflow processes. The engine reads
workflows with `frappe.get_all` (`triggers.py`, `drain.py`, both carrying `authz-ok: tier-a`), which
bypasses permissions entirely because a background job has no user. `permission_query_conditions` is
consulted only by `get_list`. Who may SEE a workflow and whose leads it ACTS ON are different questions.

Run:
    bench --site dev.localhost run-tests --module tatva_connect.tests.access.test_workflow_definition_is_grain_scoped
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import visibility
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist

WORKFLOW_DT = "CRM Workflow"
SWITCH = "Workflow::CRM Workflow::visibility"

_MINE = GRAINS[2]      # Tatvapractice · India · Field-Sales
_THEIRS = GRAINS[0]    # Goodflip-Care · Anaya · Nivolumab — no axis in common with _MINE
USER = "wfgrain.probe@example.test"


def _rows():
	"""The four shapes the predicate must tell apart, as the plain dicts it reads."""
	return {
		"mine": frappe._dict(name="wf-mine", trigger_vertical=_MINE["vertical"],
		                     trigger_group=_MINE["group"], trigger_program=_MINE["program"]),
		"theirs": frappe._dict(name="wf-theirs", trigger_vertical=_THEIRS["vertical"],
		                       trigger_group=_THEIRS["group"], trigger_program=_THEIRS["program"]),
		# A blank axis is a WILDCARD: this is "every group in my vertical", not "the empty group".
		"wildcard": frappe._dict(name="wf-wildcard", trigger_vertical=_MINE["vertical"],
		                         trigger_group="", trigger_program=""),
		# No axis at all — site-wide, shown to everyone, asked of nobody's entitlement.
		"siteWide": frappe._dict(name="wf-site", trigger_vertical="", trigger_group="", trigger_program=""),
	}


class _Case(FrappeTestCase):
	"""The switch is forced ON for the predicate tests. It ships OFF and the last class proves what OFF
	means — arming it here rather than in the DB keeps the bench's own switches untouched."""

	def setUp(self):
		super().setUp()
		assert_masters_exist()
		self._enabled = visibility.automation.is_enabled
		visibility.automation.is_enabled = lambda key: key == SWITCH

	def tearDown(self):
		visibility.automation.is_enabled = self._enabled
		super().tearDown()

	def _entitled_to(self, grain):
		"""Answer entitlement as though the caller held exactly this one grain. Patched at the seam the
		predicate really calls, so a change of predicate cannot silently stop consulting entitlement."""
		from tatva_connect.access import entitlement

		def overlaps(rule_grain, user=None):
			return all(
				not axis or axis.casefold() == (grain[i] or "").casefold()
				for i, axis in enumerate(rule_grain)
			)

		self._real = entitlement.grain_overlaps_entitlement
		entitlement.grain_overlaps_entitlement = overlaps
		self.addCleanup(setattr, entitlement, "grain_overlaps_entitlement", self._real)


class TestTheFourGrainCases(_Case):
	def setUp(self):
		super().setUp()
		self._entitled_to((_MINE["vertical"], _MINE["group"], _MINE["program"]))

	def test_a_workflow_on_my_line_is_visible(self):
		self.assertTrue(visibility.row_admits(_rows()["mine"], WORKFLOW_DT, USER))

	def test_a_workflow_on_another_line_is_hidden(self):
		self.assertFalse(
			visibility.row_admits(_rows()["theirs"], WORKFLOW_DT, USER),
			"a rep must not read how another business line runs",
		)

	def test_a_blank_axis_is_a_wildcard_not_an_empty_string(self):
		"""THE defect this whole rule exists to avoid. `Tatvapractice / <any> / <any>` covers a user in
		Tatvapractice/India/Field-Sales. Comparing the blank as "" would hide it and look correct."""
		self.assertTrue(
			visibility.row_admits(_rows()["wildcard"], WORKFLOW_DT, USER),
			"a vertical-wide workflow must reach every group inside that vertical",
		)

	def test_a_workflow_declaring_no_line_is_visible_to_everyone(self):
		"""Deliberate, and the same answer a Smart View with no axes gets: site-wide, asked of nobody's
		entitlement. Proven with a caller entitled to a DIFFERENT line, or it proves nothing."""
		self._entitled_to((_THEIRS["vertical"], _THEIRS["group"], _THEIRS["program"]))
		self.assertTrue(visibility.row_admits(_rows()["siteWide"], WORKFLOW_DT, USER))

	def test_a_system_manager_sees_everything(self):
		"""Privilege is asked BEFORE the grain, so an operator never depends on holding an entitlement."""
		for key, row in _rows().items():
			with self.subTest(row=key):
				self.assertTrue(visibility.row_admits(row, WORKFLOW_DT, "Administrator"))


class TestTheSwitchIsTheContract(_Case):
	def test_switch_off_is_stock_behaviour(self):
		"""Fail-OPEN on the switch is this seam's contract, not a bug: dormant-by-default means the app
		behaves exactly as stock crm until an operator arms it."""
		visibility.automation.is_enabled = lambda key: False
		self.assertEqual(visibility.scoped_pqc(WORKFLOW_DT, USER), "",
		                 "off must add no conditions at all")
		self.assertTrue(visibility.scoped_has_permission(_rows()["theirs"], "read", USER),
		                "off must not deny a single doc either")

	def test_on_and_entitled_to_nothing_selects_nothing_not_everything(self):
		"""The fail-closed half. An empty readable set must become `1=0`; returning "" would silently
		widen a scoped list to the whole table."""
		self._entitled_to(("no-such-vertical", "no-such-group", "no-such-program"))
		clause = visibility.scoped_pqc(WORKFLOW_DT, USER)
		self.assertTrue(clause == "1=0" or "`name` in (" in clause)

	def test_the_clause_really_runs(self):
		self._entitled_to((_MINE["vertical"], _MINE["group"], _MINE["program"]))
		clause = visibility.scoped_pqc(WORKFLOW_DT, USER)
		frappe.db.sql(f"select name from `tab{WORKFLOW_DT}` where {clause} limit 1")

	def test_the_switch_is_declared_so_the_seed_can_create_its_row(self):
		"""`seed.sync_catalog` PRUNES any row whose key left the registry, so an undeclared switch can
		never be armed — which is exactly how the three sibling doctypes stayed unscoped for months."""
		from tatva_connect.automation.registry import AUTOMATIONS

		self.assertIn(SWITCH, {a.key for a in AUTOMATIONS})


class TestScopingTheListDoesNotChangeWhatTheEngineProcesses(FrappeTestCase):
	"""The expensive mistake available here, locked. `permission_query_conditions` is consulted only by
	`get_list`; the engine reads with `get_all` and no user. Conflating the two would stop the engine dead
	in a background job — where there is nobody to be entitled."""

	def test_no_engine_path_reads_workflows_through_get_list(self):
		import ast
		import pathlib

		root = pathlib.Path(frappe.get_app_path("tatva_connect"))
		offenders = []
		for path in sorted((root / "workflow_engine").rglob("*.py")):
			if "/tests/" in str(path):
				continue
			for node in ast.walk(ast.parse(path.read_text())):
				if not isinstance(node, ast.Call):
					continue
				func = node.func
				name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
				if name != "get_list":
					continue
				first = node.args[0] if node.args else None
				if isinstance(first, ast.Constant) and first.value == WORKFLOW_DT:
					offenders.append(f"{path.name}:{node.lineno}")
		self.assertEqual(
			offenders, [],
			f"the engine would start obeying a USER's scope in a job that has no user: {offenders}",
		)
