# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W8.1 + W8.2 — ONE Update Field node writes MANY fields, each row carrying its own mode.

WHY THIS IS A NET DELETION. The decision "what does this mode MEAN" was written four times — three
byte-identical copies in `sends` (WhatsApp, Email, Voice) and a fourth in `actions` with a third branch —
and the node needed four params and three `depends_on_value` gates to ask for one field. All four
resolvers are now `contract.resolve_row`, and the node presents two controls whatever any row says.

WHAT IS BEING PROVEN, and each has its own red:

  * many fields in one node, each row with its own mode      — was: ValueError, the node had one `fieldname`
  * a rule authored in the OLD shape still writes what it wrote  — the patch, and a break-test behind it
  * Increment ADDS instead of overwriting                    — was: the mode fell through to the literal, so
                                                              "add 1" WROTE 1 over a counter sitting at 2
  * two journeys on one lead both land                       — driven with two real connections and a
                                                              barrier, never reasoned about
  * a send verb REFUSES Increment                            — was: it sent the author's step as text
  * publish still refuses a row naming a forbidden field     — per row, through the same `is_set_declared`

Nothing here arms anything. No provider is reached: the send-refusal test drives `sends._filled_rows`,
which raises before any adapter is asked for a slot name.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_update_field_rows
"""
import threading
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions, sends
from tatva_connect.patches import fold_update_field_into_rows as fold
from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.tests.automation import field_allowlist
from tatva_connect.workflow_engine import contract, graph, refs
from tatva_connect.workflow_engine.tests import fixtures as fx

# Three PLAIN columns on CRM Lead — two Data and an Int. Deliberately not `status`: it is a Link, and a
# LinkValidationError on an invented value would fail these tests for a reason that has nothing to do with
# rows. What is under test is that many fields land, not what a lead status may be.
_COUNTER = "custom_per_day_opd_size"  # a plain Int — nothing derives it
_FIRST = "first_name"
_LAST = "last_name"


def _action(**config):
	"""A verb's params as the interpreter hands them over — `frappe._dict(_config(node))`, nothing more."""
	return frappe._dict(action_type="Update Field", **config)


def _row(name, mode, value):
	return {"name": name, "mode": mode, "value": value}


class _RowsBase(FrappeTestCase):
	"""One grain-stamped lead and the three fields this suite writes, allowlisted at that grain."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		for fieldname in (_FIRST, _LAST, _COUNTER):
			field_allowlist.seed_settable(
				"CRM Lead", fieldname,
				vertical=fx.GRAIN["vertical"], group=fx.GRAIN["group"], program=fx.GRAIN["program"],
			)
		cls.lead = fx.make_lead()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		field_allowlist.clear("CRM Lead")
		frappe.db.commit()
		super().tearDownClass()

	def _reread(self, fieldname):
		"""The value as it really landed, off the row rather than off the doc the handler held."""
		return frappe.db.get_value("CRM Lead", self.lead.name, fieldname)


class TestOneNodeWritesManyFields(_RowsBase):
	"""The headline. RED before: `ValueError: Set Field action missing target doctype or fieldname` — the
	node read a single `fieldname` and a rows config named none."""

	def test_three_rows_write_three_fields_in_one_node(self):
		actions._action_set_field(
			_action(target_doctype="CRM Lead", updates=[
				_row(_FIRST, refs.LITERAL, "Asha"),
				_row(_LAST, refs.FROM_CONTEXT, "seed.surname"),
				_row(_COUNTER, refs.EXPRESSION, "2 + 3"),
			]),
			self.lead.name, {"seed.surname": "Rao"}, fx.AXES, None,
		)
		self.assertEqual(self._reread(_FIRST), "Asha")
		self.assertEqual(self._reread(_LAST), "Rao")
		self.assertEqual(self._reread(_COUNTER), 5)

	def test_each_row_carries_its_own_mode(self):
		"""The point of putting the mode on the ROW: two rows, filled two different ways, one node. If the
		mode lived on the node this could not be expressed at all."""
		actions._action_set_field(
			_action(target_doctype="CRM Lead", updates=[
				_row(_FIRST, refs.LITERAL, "Meera"),
				_row(_LAST, refs.FROM_CONTEXT, "seed.surname"),
			]),
			self.lead.name, {"seed.surname": "Singh"}, fx.AXES, None,
		)
		self.assertEqual((self._reread(_FIRST), self._reread(_LAST)), ("Meera", "Singh"))

	def test_a_forbidden_row_writes_nothing_at_all(self):
		"""Every row is gated BEFORE anything is written, so a bad third row cannot leave the first two
		applied. A per-row check inside the write loop would have."""
		before = self._reread(_FIRST)
		with self.assertRaises(PermissionError):
			actions._action_set_field(
				_action(target_doctype="CRM Lead", updates=[
					_row(_FIRST, refs.LITERAL, "Asha"),
					_row("zz_never_allowlisted", refs.LITERAL, "x"),
				]),
				self.lead.name, {}, fx.AXES, None,
			)
		self.assertEqual(self._reread(_FIRST), before, "a refused row left an earlier row applied")

	def test_a_node_with_no_rows_is_refused(self):
		with self.assertRaises(ValueError):
			actions._action_set_field(
				_action(target_doctype="CRM Lead", updates=[]), self.lead.name, {}, fx.AXES, None)


class TestARuleAuthoredInTheOldShapeStillWritesWhatItWrote(_RowsBase):
	"""The non-negotiable. A single-field node saved before W8.1 must behave IDENTICALLY after it.

	The fold is a PATCH, not a runtime reader, because `CRM Workflow Node.config_json` is mutable and a
	reader would be a second shape the runtime has to know about for ever. What a patch cannot reach is a
	frozen `CRM Workflow Version`; that node raises loudly rather than writing nothing, and the last test
	here is that raise.
	"""

	def _folded(self, config):
		return fold._folded(config)

	def test_a_literal_node_folds_to_the_same_write(self):
		folded = self._folded({
			"target_doctype": "CRM Lead", "fieldname": _FIRST, "value_mode": refs.LITERAL, "value": "Asha",
		})
		self.assertEqual(folded["updates"], [_row(_FIRST, refs.LITERAL, "Asha")])
		actions._action_set_field(_action(**folded), self.lead.name, {}, fx.AXES, None)
		self.assertEqual(self._reread(_FIRST), "Asha")

	def test_a_from_context_node_keeps_reading_the_same_variable(self):
		"""The value moves out of `context_field` and into the row's one `value`; the mode is what says how
		to read it. Getting this backwards would write the variable's NAME onto a patient's field."""
		folded = self._folded({
			"target_doctype": "CRM Lead", "fieldname": _LAST,
			"value_mode": refs.FROM_CONTEXT, "context_field": "seed.surname", "value": "the-literal",
		})
		self.assertEqual(folded["updates"], [_row(_LAST, refs.FROM_CONTEXT, "seed.surname")])
		actions._action_set_field(_action(**folded), self.lead.name, {"seed.surname": "Rao"}, fx.AXES, None)
		self.assertEqual(self._reread(_LAST), "Rao")

	def test_an_expression_node_keeps_computing(self):
		folded = self._folded({
			"target_doctype": "CRM Lead", "fieldname": _COUNTER,
			"value_mode": refs.EXPRESSION, "expression": "4 + 4", "value": "9",
		})
		self.assertEqual(folded["updates"], [_row(_COUNTER, refs.EXPRESSION, "4 + 4")])
		actions._action_set_field(_action(**folded), self.lead.name, {}, fx.AXES, None)
		self.assertEqual(self._reread(_COUNTER), 8)

	def test_a_node_with_no_declared_mode_folds_to_a_literal(self):
		"""`value_mode` was `reqd` but a draft could be saved without it, and the old runtime treated the
		absence as Literal — `_resolve_set_field_value` fell through to `return action.value`."""
		folded = self._folded({"target_doctype": "CRM Lead", "fieldname": _FIRST, "value": "Asha"})
		self.assertEqual(folded["updates"], [_row(_FIRST, refs.LITERAL, "Asha")])

	def test_the_legacy_params_are_gone_from_the_folded_config(self):
		"""They are not left beside the rows: two ways to say one thing is the second brain the row shape
		exists to remove, and a stale `fieldname` would keep the loud raise below armed for ever."""
		folded = self._folded({
			"target_doctype": "CRM Lead", "fieldname": _FIRST, "value_mode": refs.LITERAL, "value": "Asha",
		})
		for legacy in ("fieldname", "value_mode", "value", "context_field", "expression"):
			self.assertNotIn(legacy, folded)
		self.assertEqual(folded["target_doctype"], "CRM Lead", "an unrelated param was dropped with them")

	def test_folding_is_idempotent_and_leaves_a_new_shape_node_alone(self):
		rows = [_row(_FIRST, refs.LITERAL, "Asha")]
		self.assertIsNone(self._folded({"target_doctype": "CRM Lead", "updates": rows}))
		self.assertIsNone(self._folded({"target_doctype": "CRM Lead"}))

	def test_the_patch_really_rewrites_an_authored_node(self):
		"""End to end through `execute()`, on a real row, so the query and the fold are proven together.

		The old shape is written STRAIGHT ONTO THE COLUMN, and that is faithful rather than a shortcut:
		`CRMWorkflowNode.validate` now refuses it outright ("Update Field does not take a setting called
		fieldname"), so after this change no controller can create one. A pre-W8.1 site does not have to —
		its rows were saved when the declaration still accepted them, and what the patch meets on migrate is
		exactly this: a row in the database, not a document being saved.
		"""
		workflow = "w8-fold-probe"
		fx.purge(workflow)
		self.addCleanup(fx.purge, workflow)
		fx.make_workflow(workflow, [
			fx.trigger(to="n1"),
			fx.node("n1", "Update Field", edges={"next": "end"}, config={
				"target_doctype": "CRM Lead",
				"updates": [_row(_FIRST, refs.LITERAL, "Asha")],
			}),
			fx.node("end", "Terminal"),
		])
		node = frappe.db.get_value("CRM Workflow Node", {"workflow": workflow, "node_id": "n1"}, "name")
		frappe.db.set_value("CRM Workflow Node", node, "config_json", frappe.as_json({
			"target_doctype": "CRM Lead", "fieldname": _FIRST,
			"value_mode": refs.LITERAL, "value": "Asha",
		}), update_modified=False)
		frappe.db.commit()

		fold.execute()

		config = frappe.parse_json(
			frappe.db.get_value("CRM Workflow Node", {"workflow": workflow, "node_id": "n1"}, "config_json"))
		self.assertEqual(config["updates"], [_row(_FIRST, refs.LITERAL, "Asha")])
		self.assertNotIn("fieldname", config)

	def test_a_node_frozen_in_the_old_shape_fails_loudly_rather_than_writing_nothing(self):
		"""A `CRM Workflow Version` is content-addressed and immutable, so the patch cannot reach it and a
		parked journey executes what was frozen. Reading `updates` and finding none would write NOTHING and
		say nothing — on a live record, which is the failure class this engine removes everywhere else."""
		with self.assertRaises(ValueError) as raised:
			actions._action_set_field(
				_action(target_doctype="CRM Lead", fieldname=_FIRST, value_mode=refs.LITERAL, value="Asha"),
				self.lead.name, {}, fx.AXES, None,
			)
		self.assertIn("republish", str(raised.exception))


class _CounterBase(_RowsBase):
	"""A counter that starts each test at a KNOWN committed value and is put back afterwards.

	Committed, because the concurrency tests read it from other connections, and `addCleanup` rather than
	`tearDown` so a failed assertion still restores it — a counter left dirty is a live config change, and
	the next test reads a lie.
	"""

	START = 2

	def setUp(self):
		super().setUp()
		frappe.db.set_value("CRM Lead", self.lead.name, _COUNTER, self.START)
		frappe.db.commit()
		self.addCleanup(self._reset_counter)

	def _reset_counter(self):
		frappe.db.set_value("CRM Lead", self.lead.name, _COUNTER, 0)
		frappe.db.commit()

	def _increment(self, by):
		actions._action_set_field(
			_action(target_doctype="CRM Lead", updates=[_row(_COUNTER, refs.INCREMENT, by)]),
			self.lead.name, {}, fx.AXES, None,
		)


class TestIncrementAddsInsteadOfOverwriting(_CounterBase):
	"""W8.2. RED before: the mode was not declared, so `_resolve_set_field_value` fell through to its
	literal branch and 'add 1' WROTE 1 over a counter sitting at 2."""

	def test_add_one_to_two_is_three(self):
		self._increment(1)
		self.assertEqual(self._reread(_COUNTER), 3)

	def test_an_unset_counter_starts_at_zero(self):
		"""An operator saying 'add one' to a counter nobody has set yet means one, never an error.

		Two halves, because the column cannot hold the interesting one: an Int on CRM Lead is NOT NULL
		(`(1048, "Column 'custom_per_day_opd_size' cannot be null")` when this test first tried), so `0` is
		what unset looks like on the record and `None` is what it looks like coming off a field that CAN be
		null. Both must add rather than raise.
		"""
		frappe.db.set_value("CRM Lead", self.lead.name, _COUNTER, 0)
		frappe.db.commit()
		self._increment(1)
		self.assertEqual(self._reread(_COUNTER), 1)
		self.assertEqual(contract.resolve_row(refs.INCREMENT, 1, {}, current=None), 1)

	def test_it_lands_as_the_fields_own_type(self):
		"""`flt` is what adds, so the sum is a float; the Int column stores an int because frappe's own
		`_fix_numeric_types` casts by fieldtype on save. Nothing here casts by hand."""
		self._increment(1)
		self.assertIsInstance(self._reread(_COUNTER), int)

	def test_the_mode_is_the_declared_word_and_the_runtime_follows_it(self):
		"""Rename the one declaration and a runtime holding its own copy cannot follow — it would fall
		through to the literal branch and overwrite the counter with the step."""
		renamed = "ZZ Renamed Increment"
		with patch.object(refs, "INCREMENT", renamed):
			self.assertEqual(contract.resolve_row(renamed, 1, {}, current=2), 3)

	def test_increment_is_offered_only_where_there_is_a_field_to_add_to(self):
		"""The declaration half. Increment is offered exactly where a CURRENT value exists to add to — the
		lead's own field, and the row an upsert writes into. Everywhere else keeps the plain modes: a
		template slot has no target at all, and an append creates a new row, which has nothing to add to.
		"""
		from tatva_connect.workflow_engine import registry

		offered = {}
		for entry in registry.node_types():
			for field in entry["config"]:
				if registry.read_kind_of(field) == "value_rows":
					offered[(entry["type"], field["name"])] = field.get("modes")
		for key in _WRITES_OVER_A_CURRENT_VALUE:
			with self.subTest(field=key):
				self.assertEqual(offered[key],
				                 [refs.LITERAL, refs.FROM_CONTEXT, refs.EXPRESSION, refs.INCREMENT])
		for key, modes in offered.items():
			if key not in _WRITES_OVER_A_CURRENT_VALUE:
				with self.subTest(field=key):
					self.assertNotIn(refs.INCREMENT, modes)


# The two fields whose write lands on a value that already exists — the only place adding to it means
# anything. `Upsert Child Row` qualifies because it writes the lead's EXISTING row in a section; `Append
# Child Row` does not, because the row it makes has no current value.
_WRITES_OVER_A_CURRENT_VALUE = {("Update Field", "updates"), ("Upsert Child Row", "set_fields")}


class TestASendVerbRefusesIncrement(_RowsBase):
	"""The refusal is EXPLICIT, not incidental. RED before: an unrecognised mode fell through to the
	literal branch, so `Increment by 1` on a template slot sent the patient the character `1`."""

	def test_filling_a_template_slot_with_increment_raises(self):
		with self.assertRaises(ValueError) as raised:
			sends._filled_rows(["1"], [_row("1", refs.INCREMENT, "1")], {}, "Send WhatsApp: template t")
		self.assertIn(refs.INCREMENT, str(raised.exception))

	def test_the_refusal_is_the_missing_target_and_not_a_missing_value(self):
		"""A counter really can be sitting at `None` or 0, so the absence of a target cannot be signalled
		by `None` — which is why `resolve_row` takes a sentinel."""
		self.assertEqual(contract.resolve_row(refs.INCREMENT, 1, {}, current=None), 1)
		self.assertEqual(contract.resolve_row(refs.INCREMENT, 1, {}, current=0), 1)
		with self.assertRaises(ValueError):
			contract.resolve_row(refs.INCREMENT, 1, {})

	def test_the_other_modes_still_fill_a_slot_exactly_as_before(self):
		filled, blank = sends._filled_rows(
			["a", "b"],
			[_row("a", refs.LITERAL, "Namaste"), _row("b", refs.FROM_CONTEXT, "crm_lead.first_name")],
			{"crm_lead.first_name": "Asha"}, "Send WhatsApp: template t",
		)
		self.assertEqual(filled, {"a": "Namaste", "b": "Asha"})
		self.assertEqual(blank, [])

	def test_a_slot_that_resolves_blank_is_reported_and_not_sent(self):
		filled, blank = sends._filled_rows(
			["a"], [_row("a", refs.FROM_CONTEXT, "crm_lead.first_name")], {"crm_lead.first_name": ""},
			"Send WhatsApp: template t",
		)
		self.assertEqual((filled, blank), ({}, ["a"]))

	def test_a_slot_with_no_row_still_raises_and_names_itself(self):
		with self.assertRaises(ValueError) as raised:
			sends._filled_rows(["patient_name"], [], {}, "Send Email: template t")
		self.assertIn("patient_name", str(raised.exception))


class TestTwoJourneysOnOneLeadBothLand(_CounterBase):
	"""W8.2's only correctness bug, DRIVEN — two real connections, not an argument about one.

	Increment is a read-modify-write, so two journeys can both read 2 and both write 3 and one patient's
	count is short. The interleaving is FORCED rather than hoped for: both threads stop at a barrier
	between the read and the write. With the row lock the second thread never reaches the barrier — it is
	waiting on `for_update` — so the first times out, commits, and the second reads what the first wrote.

	WHAT THE LOSER GETS, measured rather than assumed: MariaDB raises 1020 ER_CHECKREAD ("record has
	changed since last read"), because a locking read of a row modified after this transaction's snapshot
	cannot be served. That is not a hole — `frappe.db.is_deadlocked` counts ER_CHECKREAD alongside 1213,
	and `interpreter.py:218` already classifies `frappe.QueryDeadlockError` as TRANSIENT, rolls back to the
	last durable suspend and lets the reconciler re-drive. So the lock hands its loser to a seam that
	already exists, and the retry is what makes both land. Both halves are asserted below, and the
	exception is compared BY REFERENCE to the one the interpreter catches so a rename there goes red here.
	"""

	START = 0  # two increments of one must read as exactly 2, so the race starts from nothing

	def _race(self, barrier=None):
		"""Two journeys incrementing the same counter on their own connections. Returns their exceptions.

		The barrier sits between the read and the write and is patched onto the MODULE, so both threads pass
		through the same one. `frappe.init/connect/destroy` per thread because `frappe.local` is thread
		local: without its own connection a thread would share the test's transaction and race nothing.
		"""
		site = frappe.local.site
		raised = []
		original = actions._resolve_write_target

		def barriered(*args, **kwargs):
			doc = original(*args, **kwargs)
			if barrier is not None:
				try:
					barrier.wait()
				except threading.BrokenBarrierError:
					pass  # the lock did its job: the other thread never got here while we held the row
			return doc

		def journey():
			frappe.init(site=site)
			frappe.connect()
			try:
				frappe.db.begin()
				self._increment(1)
				frappe.db.commit()
			except Exception as e:  # collected, never swallowed — a thread that died silently proves nothing
				raised.append(e)
			finally:
				frappe.destroy()

		with patch.object(actions, "_resolve_write_target", barriered):
			threads = [threading.Thread(target=journey) for _ in range(2)]
			for t in threads:
				t.start()
			for t in threads:
				t.join(timeout=60)
		return raised

	def test_the_lock_serializes_and_the_loser_is_the_engines_transient_class(self):
		"""At most one journey is turned away, and when one is, it is turned away as the exact exception
		`interpreter.advance` retries. Compared by REFERENCE: a rename in the engine must go red here."""
		import inspect

		from tatva_connect.workflow_engine import interpreter

		barrier = threading.Barrier(2, timeout=4)
		raised = self._race(barrier=barrier)

		# THE discriminator, and the reason the barrier is not a convenience. Both threads stop between the
		# read and the write; if the second one ever ARRIVES, the two read the same counter and the lock did
		# not hold. It cannot arrive while the first holds the row, so the first times out and the barrier
		# breaks. Without the lock they meet, the barrier passes intact, and one increment is lost.
		self.assertTrue(barrier.broken,
		                "both journeys reached the read at once — the row was never locked, so they collided")
		self.assertLessEqual(len(raised), 1, "both journeys failed — nothing was serialized, they collided")
		for error in raised:
			self.assertIsInstance(error, frappe.QueryDeadlockError)
		# And that class really is the one `advance` parks as transient. A source read rather than
		# `frappe.db.is_deadlocked`, which was the wrong question: it inspects a RAW driver error's args, and
		# what reaches a caller here is `QueryDeadlockError` wrapping it, so it answers False on the very
		# exception the engine retries. `advance` matches on the TYPE, so the type is what to lock.
		self.assertIn("QueryDeadlockError", inspect.getsource(interpreter.advance),
		              "advance no longer treats a lock collision as transient, so nothing re-drives the loser")

	def test_both_increments_land_once_the_loser_re_drives(self):
		"""The outcome that matters. The reconciler re-drives a journey the interpreter parked as transient;
		here the test re-drives it, and the second increment reads what the first wrote — 2, not 1."""
		raised = self._race(barrier=threading.Barrier(2, timeout=4))
		for _ in raised:
			self._increment(1)  # what interpreter.py:218 → the reconciler does for a transient failure

		self.assertEqual(
			frappe.db.get_value("CRM Lead", self.lead.name, _COUNTER), 2,
			"two journeys incremented one counter and only one of them landed",
		)


class TestPublishStillRefusesARowItShould(_RowsBase):
	"""The gate came FREE for references — `updates` reads `value_rows`, so `contract.value_row_keys`
	already walks it. The write-target half did NOT: `_settable_problems` answers for one fieldname, so a
	rows field needed an asker that calls it per row. That is the only new gate code in this leg."""

	def _node(self, node_id, node_type, config=None, edges=None):
		"""`graph.problems` reads a node the way the DB stores one — `config_json` text and edge ROWS. The
		canvas fixture (`fx.node`) speaks the authoring shape instead, and mixing the two is how this class
		first came back with `'str' object has no attribute 'get'` rather than a verdict."""
		return {
			"node_id": node_id, "node_type": node_type,
			"config_json": frappe.as_json(config or {}),
			"edges": [{"from_output": o, "to_node": t} for o, t in (edges or {}).items()],
		}

	def _problems(self, config):
		nodes = [
			self._node("start", "Trigger", {
				"subject_doctype": "CRM Lead", "event": "Created",
				"vertical": fx.GRAIN["vertical"], "group": fx.GRAIN["group"], "program": fx.GRAIN["program"],
			}, {"next": "n1"}),
			self._node("n1", "Update Field", config, {"next": "end"}),
			self._node("end", "Terminal"),
		]
		return " | ".join(p["message"] for p in graph.problems(nodes, entry_node="start"))

	def test_a_row_naming_a_forbidden_field_is_refused(self):
		found = self._problems({
			"target_doctype": "CRM Lead",
			"updates": [_row("zz_not_settable", refs.LITERAL, "x")],
		})
		self.assertIn("zz_not_settable", found)

	def test_an_allowed_row_beside_a_forbidden_one_is_not_blamed(self):
		found = self._problems({
			"target_doctype": "CRM Lead",
			"updates": [_row(_FIRST, refs.LITERAL, "Asha"), _row("zz_not_settable", refs.LITERAL, "x")],
		})
		self.assertIn("zz_not_settable", found)
		self.assertNotIn(_FIRST, found)

	def test_a_from_context_row_naming_nothing_upstream_is_refused(self):
		"""The reference half, which arrived with the read kind and needed no code at all."""
		found = self._problems({
			"target_doctype": "CRM Lead",
			"updates": [_row(_FIRST, refs.FROM_CONTEXT, "nothing.produces_this")],
		})
		self.assertIn("nothing.produces_this", found)

	def test_the_fault_is_anchored_on_the_rows_control(self):
		found = contract.reads_of("Update Field", {
			"updates": [_row(_FIRST, refs.FROM_CONTEXT, "nope")],
		})
		self.assertEqual([r["field"] for r in found], ["updates"],
		                 "the author must be told which box to fix")


class TestTheNodeNoLongerChangesShape(_RowsBase):
	"""W8.1's other half, and the reason it is a deletion. The node asked for one field through four params
	and three `depends_on_value` gates, so it looked different depending on what the author had picked."""

	def test_update_field_declares_no_gate_at_all(self):
		for param in actions.VERBS["Update Field"]["params"]:
			with self.subTest(param=param["name"]):
				self.assertNotIn("depends_on_value", param)

	def test_it_asks_for_a_target_and_a_set_of_rows_and_nothing_else(self):
		self.assertEqual([p["name"] for p in actions.VERBS["Update Field"]["params"]],
		                 ["target_doctype", "updates"])

	def test_the_audit_label_names_the_fields_this_node_sets(self):
		"""`_action_label` read `a.fieldname`, which no longer exists — it would have degraded to
		"Update Field ?" on every node, silently, in the journey log an operator reads."""
		label = actions._action_label(_action(updates=[
			_row(_FIRST, refs.LITERAL, "Asha"), _row(_COUNTER, refs.INCREMENT, 1),
		]))
		self.assertEqual(label, f"Update Field {_FIRST}, {_COUNTER}")

	def test_the_four_resolvers_are_one(self):
		"""A source scan, narrow on purpose: the line three send builders each held. It is not a style
		check — four copies is four chances for a renamed mode to fall through to the literal branch."""
		import inspect
		import re

		copies = len(re.findall(
			r"ctx\.get\(value\) if mode ==", inspect.getsource(sends) + inspect.getsource(actions)))
		self.assertEqual(copies, 0, "a send builder still decides for itself what a mode means")
