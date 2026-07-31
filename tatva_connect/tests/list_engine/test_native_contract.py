# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The FRAMEWORK behaviour this layer stands on and does not own — pinned, because nobody here can change it.

`ListRequest.for_native()` rewrites `rows`, `kanban_fields`, `columns`, `column_field` and `order_by`. It
does NOT rewrite the saved view's `group_by_field`, and that is deliberate: native reads that key itself,
appends it to `rows` when absent (`crm/api/doc.py:355`) and hands the lot to `frappe.get_list(fields=rows)`
(`:358`). So on the group-by surface a derived NAME genuinely reaches `fields=`, on every request, and the
surface works for exactly one reason — frappe DROPS a field it cannot resolve and returns the row without
it. Undocumented, measured on frappe 16.22.0, and load-bearing for every rep who groups by Task Status.

(The earlier reading of this defect named `doc.py:331`, the saved-view re-read. That branch is unreachable:
`custom_view` is set true whenever the client sends `columns` or `rows` (`:309`), which `getParams()`
always does. A test there would pin nothing, so there is none.)

Nothing below tests OUR code, and none of it can be red on today's app — these are characterization tests
of the framework. They are green the day they are written and go red the day frappe changes under us,
which is the only day they matter. Should `get_list` ever start THROWING on an unresolvable field instead
of dropping it, every rep's group-by view goes blank or 500s; the failures here say that in words, and
name the one line that would then have to translate `group_by_field` too.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.list_engine.test_native_contract
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime, nowdate

from tatva_connect.api import list_link_titles
from tatva_connect.list_engine import fields

TASK = "CRM Task"
PROBE = "NativeContractProbe"
FIELD = fields.DUE_STATE.fieldname

# The columns the group-by page really asks for; the derived name is NOT among them, so the only thing
# putting it into `fields=` is native's own append at doc.py:355 — which is what this module pins.
REAL_ROWS = ["name", "title", "status", "due_date"]

_WHY_IT_MATTERS = (
	f"`{FIELD}` reaches `frappe.get_list(fields=...)` on EVERY group-by request: crm/api/doc.py:355 appends "
	"the saved view's group_by_field to `rows` and :358 passes rows straight to get_list, and "
	"ListRequest.for_native() does not rewrite that key. The surface works only because frappe drops what "
	"it cannot resolve. If that has changed, every rep's group-by view on a derived field is now blank or a "
	"500, and for_native() must translate `group_by_field` the way it already translates `rows`."
)


class NativeContractCase(FrappeTestCase):
	"""One row in every bucket the declaration has, scoped by title so the answers stay deterministic."""

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete(TASK, {"title": ["like", f"{PROBE}%"]})
		now = now_datetime()
		self.expected = {
			"overdue": ("Todo", add_to_date(now, minutes=-1), "Overdue"),
			"due_end_of_today": ("Backlog", f"{nowdate()} 23:59:59", "Due Today"),
			"upcoming": ("Todo", add_to_date(now, days=1), "Upcoming"),
			"no_due_date": ("Todo", None, "No Due Date"),
			"history": ("Done", add_to_date(now, days=-3), "History"),
		}
		self.names = {}
		for label, (status, due, _value) in self.expected.items():
			doc = frappe.get_doc(
				{"doctype": TASK, "title": f"{PROBE} {label}", "status": status, "due_date": due}
			).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
			self.names[label] = doc.name
		self.scope = {"title": ["like", f"{PROBE}%"]}

	def tearDown(self):
		frappe.db.delete(TASK, {"title": ["like", f"{PROBE}%"]})

	def _payload(self, **overrides):
		payload = {
			"doctype": TASK,
			"filters": dict(self.scope),
			"order_by": "creation asc",
			"rows": list(REAL_ROWS),
			"page_length": 50,
			"view": {"view_type": "group_by", "group_by_field": FIELD},
		}
		payload.update(overrides)
		return payload


class TestFrappeDropsAnUnresolvableField(NativeContractCase):
	def test_get_list_drops_it_rather_than_throwing(self):
		try:
			rows = frappe.get_list(TASK, fields=[*REAL_ROWS, FIELD], filters=self.scope, limit=0)
		except Exception as thrown:
			self.fail(f"frappe.get_list raised {thrown!r} instead of dropping `{FIELD}`. {_WHY_IT_MATTERS}")

		self.assertEqual(len(rows), len(self.names), "the fixture rows did not come back at all")
		for row in rows:
			self.assertNotIn(
				FIELD, row, f"frappe now RESOLVES `{FIELD}` in fields= — a real column has grown behind it"
			)
			for column in REAL_ROWS:
				self.assertIn(column, row, f"`{column}` was lost when `{FIELD}` was asked for alongside it")

	def test_asking_for_it_changes_nothing_else_about_the_answer(self):
		"""Dropping it silently is only safe while it is the ONLY thing dropped — same rows, same values."""
		plain = frappe.get_list(
			TASK, fields=list(REAL_ROWS), filters=self.scope, order_by="creation asc", limit=0
		)
		alongside = frappe.get_list(
			TASK, fields=[*REAL_ROWS, FIELD], filters=self.scope, order_by="creation asc", limit=0
		)
		self.assertEqual(
			frappe.as_json(alongside),
			frappe.as_json(plain),
			f"asking for `{FIELD}` alongside real columns no longer returns the same rows. {_WHY_IT_MATTERS}",
		)


class TestTheGroupByRequestPath(NativeContractCase):
	"""The bare `get_list` pin above is only load-bearing if the request path really reaches it. It does."""

	def test_for_native_still_hands_native_the_derived_group_by_field(self):
		from tatva_connect.list_engine.engine import ListRequest

		request = ListRequest(self._payload())
		self.assertTrue(request.named, f"the view's group_by_field no longer names `{FIELD}` to the engine")
		self.assertFalse(
			request.changes_the_query, "a group-by is a DISPLAY change; taking the query path would be new"
		)
		view = frappe.parse_json(request.for_native().get("view") or "{}")
		self.assertEqual(
			view.get("group_by_field"),
			FIELD,
			f"for_native() now rewrites `group_by_field`, so the drop above is no longer what carries this "
			f"surface — re-read whether crm/api/doc.py:355 still sees `{FIELD}`",
		)

	def test_the_group_by_surface_answers_a_full_page_over_that_drop(self):
		result = list_link_titles.get_data(**self._payload())
		shown = {r["name"]: r.get(FIELD) for r in result["data"]}
		self.assertEqual(
			set(shown), set(self.names.values()), f"the group-by page came back short. {_WHY_IT_MATTERS}"
		)
		for label, (_status, _due, value) in self.expected.items():
			self.assertEqual(shown[self.names[label]], value, label)
		for row in result["data"]:
			self.assertIn("due_date", row, f"a real column was lost on the way past `{FIELD}`")
