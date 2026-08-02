# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A module on the posture seam may not grow a second answer to "must this caller prove permission?".

THE DEFECT THIS LOCKS OUT, in its own words. The activity brain asked that question at nine call sites.
Three of them tested the trusted posture first; six went straight to the permission engine. Nothing was
wrong with any individual line — the wrong thing was that the question had nine answers, so which sites
knew about the partner was decided by which sites a partner had happened to reach. The one that had not
been reached yet (`lead_field_values`, on the write path of every activity type declaring a
`source = Lead` field) threw a bare `PermissionError` carrying no message, which the partner API renders
as a 403 reading "The request was refused and no reason was recorded". Two of twelve live Anaya activity
types were unusable and the refusal said nothing about why.

Adding a tenth guard would have left the same trap for the eleventh. So the rule moved into
`access/posture.py` and the call sites lost the ability to state it. This lock is what keeps them from
getting it back.

THE ROSTER, NOT A BAN. `ON_THE_SEAM` is the list of modules that have been brought onto the seam — the
ones on a code path a server-opened trusted block can reach. It is NOT an app-wide prohibition:
`frappe.has_permission` is the right call everywhere the trusted posture cannot occur, and most of the app
is such a place. Bringing a module on means adding it here in the same commit, so the roster and the code
cannot drift apart.

Two things are forbidden inside a roster module, and they are the two halves of the same mistake:
  * calling `frappe.has_permission` — a permission decision taken outside the checkpoint;
  * reading `frappe.flags.ignore_permissions` — a posture decision taken outside the checkpoint. A caller
    that reads the raw flag has re-implemented `is_trusted()`, and the next reader will spell it
    differently (`if not frappe.flags...` vs `if frappe.flags...` guarding the opposite branch).

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.architecture.test_permission_checks_one_seam
"""
import ast
import pathlib

import frappe
from frappe.tests.utils import FrappeTestCase

APP = pathlib.Path(frappe.get_app_path("tatva_connect"))

# The seam itself: the ONE module allowed to call the engine and to read the flag.
SEAM = "access/posture.py"

# Modules brought onto the seam. Add a module here in the SAME commit that converts its checks.
ON_THE_SEAM = (
	"activity/api.py",
	"lead/detail.py",
)

# The context manager that SETS the posture. It necessarily writes the flag the seam reads, and it lives
# in a module this agent does not own; it is named here so the pair is visible in one place rather than
# discovered by whoever next wonders where "trusted" comes from.
POSTURE_SETTER = "api/_base.py"


def _tree(rel):
	return ast.parse((APP / rel).read_text(), filename=rel)


def _is_frappe_attr(node, *path):
	"""True if `node` is the attribute chain `frappe.<path...>` — e.g. frappe.flags.ignore_permissions."""
	for name in reversed(path):
		if not (isinstance(node, ast.Attribute) and node.attr == name):
			return False
		node = node.value
	return isinstance(node, ast.Name) and node.id == "frappe"


def _engine_calls(tree):
	"""Every `frappe.has_permission(...)` call site, as line numbers."""
	return [n.lineno for n in ast.walk(tree)
			if isinstance(n, ast.Call) and _is_frappe_attr(n.func, "has_permission")]


def _flag_reads(tree):
	"""Every mention of `frappe.flags.ignore_permissions`, as line numbers."""
	return [n.lineno for n in ast.walk(tree)
			if isinstance(n, ast.Attribute) and _is_frappe_attr(n, "flags", "ignore_permissions")]


class TestPermissionChecksOneSeam(FrappeTestCase):
	def test_the_seam_exists_and_is_the_one_place_that_asks_the_engine(self):
		"""The premise. If `require` stopped calling the engine, every test below would pass while the
		roster modules checked nothing at all — the failure mode a roster is most exposed to."""
		tree = _tree(SEAM)
		self.assertTrue(_engine_calls(tree), f"{SEAM} no longer asks the permission engine at all")
		self.assertTrue(_flag_reads(tree), f"{SEAM} no longer reads the posture flag")

	def test_no_roster_module_asks_the_engine_directly(self):
		hits = []
		for rel in ON_THE_SEAM:
			hits += [f"{rel}:{line}" for line in _engine_calls(_tree(rel))]
		self.assertEqual(hits, [], (
			"a module on the posture seam calls frappe.has_permission directly — it must call "
			f"access.posture.require, which is the only place that knows about the trusted block: {hits}"
		))

	def test_no_roster_module_reads_the_posture_flag_directly(self):
		hits = []
		for rel in ON_THE_SEAM:
			hits += [f"{rel}:{line}" for line in _flag_reads(_tree(rel))]
		self.assertEqual(hits, [], (
			"a module on the posture seam reads frappe.flags.ignore_permissions directly — it must call "
			f"access.posture.is_trusted(), so 'trusted' means one thing everywhere: {hits}"
		))

	def test_the_setter_and_the_reader_name_the_same_flag(self):
		"""The seam is split across two modules — `_base.trusted_permissions()` writes the flag,
		`access.posture` reads it. A rename on either side would silently stop the bypass applying and
		every partner activity carrying a lead-sourced field would 403 again with no reason recorded.
		The behavioural half of this lock is
		tests/activity/test_trusted_posture_one_seam.py::test_the_setter_and_the_reader_agree."""
		self.assertTrue(_flag_reads(_tree(POSTURE_SETTER)),
						f"{POSTURE_SETTER} no longer sets the flag {SEAM} reads")
