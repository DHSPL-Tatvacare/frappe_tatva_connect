# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The reader and the writer must route an activity field by the SAME rule.

`activity/api.py` declares that rule once — `field_column(f)`: the field's `target` is a promoted CRM
Task column if it is one of `PROMOTED_COLUMNS`, and otherwise the field lives in the JSON payload under
its schema fieldname. `compute_activity` (write), `set_schema_field` (write) and `smartview/api.py`
(project) all ask it.

`_task_values` — the READ side, which re-keys a saved task back to schema fieldnames — did not. It read
`f.get("target")` raw and did `r.get(target)`. `CRM Task Type Field.target` is an unconstrained `Data`
field: no options, no validation. So an operator may declare a `target` that names a real CRM Task
column which is NOT promoted — and then the two sides disagree about where the answer lives.

When that column is also one the reader SELECTs (`status`, `assigned_to`, `description`, `owner` —
api.py:414, :501, :636), the disagreement is not a blank: the reader hands back the TASK'S OWN unrelated
column value in place of the rep's answer. A "Patient Consented" reads back as "Todo".

Phase 0 of docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md. §7 makes this seam the one
every consumer resolves through, so it must be coherent before it is widened.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.activity.test_field_routing_coherence
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.tests.activity import task_type_fixture

TYPE_NAME = "ZZ Routing Coherence Probe"

# Two targets that name a REAL CRM Task column the reader SELECTs but that the writer does not promote.
# This is the whole defect surface: a target the two sides read differently AND that carries a value.
DIVERGENT_TARGETS = ("status", "assigned_to")

# A promoted field and a payload field alongside them — a fixture carrying only the divergent shape
# could not tell a fixed reader from one that simply stopped reading columns.
SCHEMA = (
	{"label": "ZZ Visit Status", "fieldname": "zz_visit_status", "fieldtype": "Data", "target": "status"},
	{"label": "ZZ Handled By", "fieldname": "zz_handled_by", "fieldtype": "Data", "target": "assigned_to"},
	{"label": "ZZ Outcome", "fieldname": "zz_outcome", "fieldtype": "Data", "target": "custom_outcome"},
	{"label": "ZZ Remark", "fieldname": "zz_remark", "fieldtype": "Small Text"},
)

SUBMITTED = {
	"zz_visit_status": "ZZ Patient Consented",
	"zz_handled_by": "ZZ Field Rep",
	"zz_outcome": "ZZ Reached",
	"zz_remark": "ZZ remark text",
}


class TestFieldRoutingCoherence(FrappeTestCase):
	"""One routing rule, asked by both sides — write through `field_column`, read through `field_column`."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.task_type = task_type_fixture.mint_type(TYPE_NAME, SCHEMA)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Routing Coherence Probe",
			"mobile_no": f"+9198129{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": task_type_fixture.VERTICAL, "custom_group": task_type_fixture.GROUP,
		}).insert(ignore_permissions=True)

	def test_the_probe_targets_are_real_non_promoted_task_columns(self):
		"""The premise. A target naming no real column could never collide, and one inside
		PROMOTED_COLUMNS is no divergence at all — either way the fixture would prove nothing."""
		for target in DIVERGENT_TARGETS:
			self.assertTrue(frappe.db.has_column("CRM Task", target),
							f"`{target}` is not a real CRM Task column — the probe cannot collide")
			self.assertNotIn(target, activity_api.PROMOTED_COLUMNS,
							 f"`{target}` is promoted now — pick a target the writer still sends to payload")

	def test_every_saved_answer_reads_back_as_the_answer(self):
		"""The property, on the paths the rep actually uses: `save_activity` writes, `task_detail` reads.
		For EVERY field the type declares, the place the reader looks is the place the writer put it.
		Before the fix `zz_visit_status` comes back as the task's own status ('Todo')."""
		task = activity_api.save_activity(self.lead.name, self.task_type, SUBMITTED)
		values = activity_api.task_detail(task)["task"]["values"]
		for fieldname, submitted in SUBMITTED.items():
			self.assertEqual(
				values.get(fieldname), submitted,
				f"`{fieldname}` was written to one place and read from another — the reader handed back "
				f"{values.get(fieldname)!r} where the rep answered {submitted!r}",
			)
