# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""EVERY WORKFLOW ENQUEUE GOES ON THE WORKFLOW LANE, OR THE SAFETY NET IS TIED TO THE FALLING ROCK.

W4.1 put the timer alarm on `workflow` and left the rest where they were: `start_journey` and
`resume_for_signal` on `short`, the WhatsApp delivery thunk on `default` by omission. Both worker
services consume `short` (`--queue short,default` and `--queue long,default,short`), so a burst of
workflow work starves `wakeups.sweep` - the reconciler whose entire job is rescuing runs whose wake was
lost. At cohort volume that is a stuck site, and the thing that would have rescued it is in the queue
behind the work that stuck it.

The lane already exists, is registered by the configurator and is locked by
`test_wake_lane_is_deployable`. This is the rest of the engine moving onto it.

WALKED WITH AST, never grepped: `queue="short"` inside a docstring or a comment would satisfy a regex
and change nothing at runtime.
"""
import ast
import pathlib
import unittest

from tatva_connect.workflow_engine import wakeups

_APP = pathlib.Path(__file__).parents[2]
# Where workflow background work is enqueued from. `automation/` is in scope because the engine's verbs
# live there - `sends` is reached only through `actions._action_send_whatsapp`, which is a node handler.
_LANES = ("workflow_engine", "automation")


def _enqueue_calls():
	"""Every `frappe.enqueue(...)` in the engine, as (file, line, {kwarg: node})."""
	for area in _LANES:
		for path in sorted((_APP / area).rglob("*.py")):
			if "/tests/" in str(path):
				continue
			tree = ast.parse(path.read_text())
			for node in ast.walk(tree):
				if not isinstance(node, ast.Call):
					continue
				target = node.func
				name = getattr(target, "attr", None)
				owner = getattr(getattr(target, "value", None), "id", None)
				if name != "enqueue" or owner != "frappe":
					continue
				method = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else None
				yield path.name, node.lineno, method, {kw.arg: kw.value for kw in node.keywords}


def _named_lane(node):
	"""The lane a call site names, whether it spells it or reads the shared constant.

	`queue=wakeups.WAKE_QUEUE` is the SAME answer as `queue="workflow"` and a better way to write it —
	one that cannot drift from the constant every other site compares against. A lock that only reads
	string literals calls the safer spelling an offence.
	"""
	if isinstance(node, ast.Constant):
		return node.value
	if isinstance(node, ast.Attribute) and node.attr == "WAKE_QUEUE":
		return wakeups.WAKE_QUEUE
	return None


# The one job that belongs on ANOTHER lane, named by the method it starts so the exception survives the
# line moving. A document render is seconds of a pdfkit subprocess; the engine's lane is where every
# journey is walked, and one render there stalls all of them.
_OFF_LANE_BY_DESIGN = {
	"tatva_connect.workflow_engine.document_render.render_document": "long",
}


class TestEveryEngineEnqueueNamesTheWorkflowLane(unittest.TestCase):
	"""THE red."""

	def test_no_engine_job_is_left_on_a_starved_lane(self):
		offenders = []
		for filename, lineno, method, kwargs in _enqueue_calls():
			named = _named_lane(kwargs.get("queue"))
			if named == _OFF_LANE_BY_DESIGN.get(method):
				continue
			if named != wakeups.WAKE_QUEUE:
				offenders.append(f"{filename}:{lineno} queue={named!r}")
		self.assertEqual(
			offenders, [],
			f"these ride a lane both workers consume, so they can starve the sweep: {offenders}",
		)

	def test_the_engine_really_does_enqueue_something(self):
		"""A walk that finds nothing would pass the test above by testing nothing."""
		self.assertGreaterEqual(len(list(_enqueue_calls())), 3)

	def test_the_lane_is_a_literal_this_lock_can_read(self):
		"""A computed queue name would defeat the check above by being unreadable at rest. Every site
		spells the lane, and the test above is what makes them one answer by comparing each to
		`wakeups.WAKE_QUEUE`."""
		for filename, lineno, _method, kwargs in _enqueue_calls():
			with self.subTest(site=f"{filename}:{lineno}"):
				self.assertIsNotNone(_named_lane(kwargs.get("queue")))


class TestTheJobsStayLosable(unittest.TestCase):
	"""Moving lanes must not quietly change the durability contract: the row is the truth, the job is a
	latency optimisation, and a rolled-back segment must enqueue nothing."""

	def test_every_engine_enqueue_still_fires_only_after_commit(self):
		offenders = []
		for filename, lineno, _method, kwargs in _enqueue_calls():
			after_commit = kwargs.get("enqueue_after_commit")
			if not (isinstance(after_commit, ast.Constant) and after_commit.value is True):
				offenders.append(f"{filename}:{lineno}")
		self.assertEqual(offenders, [], f"a rolled-back segment would still enqueue at: {offenders}")
