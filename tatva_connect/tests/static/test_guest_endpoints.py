# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The public attack surface — every `@frappe.whitelist(allow_guest=True)` endpoint in our app.

An `allow_guest=True` whitelisted method is callable over HTTP by an UNAUTHENTICATED caller (the
internet). That is the single scariest thing to get wrong: one stray decorator exposes an endpoint
to the world. This test scans our source (AST — no import/DB state to fool it) for every such
endpoint and asserts EACH ONE carries, AT THE CODE:

  * a `# guest-ok: <reason>` marker on/adjacent to its `@frappe.whitelist(allow_guest=True)`
    decorator — proves a human reviewed it and recorded WHY it is public;
  * a recognised SELF-GATE token in its own code (decorators + body, docstring/comment-stripped)
    — proves it actually gates itself (token/signature auth for a webhook, a file-privacy check,
    a scope-bounding helper for a lookup), not just that the marker SAYS it does.

Same idiom as the `# authz-ok:` marker in `test_no_perm_bypass.py` — shared AST/marker plumbing
lives in `_lock_helpers.py` (constitution A.8: one brain, not two). There is NO hand-maintained
allowlist and no per-endpoint test to add: a new public endpoint fails the build until it carries
both marker and gate, and the reason/gate stay readable in one `grep` beside the code they
describe — they cannot silently drift from what the code actually does.

Scope note: this guards that every public endpoint is (a) reviewed/documented and (b) self-gated
in SOME recognised way. Whether a specific gate is actually SUFFICIENT for its data (verifies a
provider token, never trusts a client-supplied grain — A.9/A.16) is asserted by the per-surface
tests (webhook spine, telephony, storage privacy) and the bypass-write audit. Here we hold the
line on "nothing public is undocumented or ungated".

Pure AST + stdlib: runs standalone under `tcsec locks` and inside `bench run-tests`.
"""
import ast
import os

try:  # This is a pure-AST source lock — it runs in the bench AND standalone (tcsec, no frappe).
	from frappe.tests.utils import FrappeTestCase
except ModuleNotFoundError:
	import unittest

	FrappeTestCase = unittest.TestCase

from tatva_connect.tests.static._lock_helpers import app_root, guard_text, is_whitelist_decorator, span_text

_APP_ROOT = app_root(__file__)

# The marker every `allow_guest=True` decorator must carry: `# guest-ok: <reason>`.
MARKER = "guest-ok:"

# Recognised SELF-GATE tokens for a PUBLIC (allow_guest) endpoint — any ONE, present in the
# function's own CODE (decorators + body; never a docstring/comment — see `guard_text`), proves
# it gates itself rather than just being marked reviewed. Derived from what our current guest
# endpoints ACTUALLY call (audit 2026-07-08) — do not add a token that isn't a real gate
# somewhere in this app; an endpoint with no real gate is a finding, not something to paper over.
#
#   spine.receive / _receive — the shared inbound-webhook front door (webhooks/spine.py): does
#     kill-switch -> constant-time token auth + account scoping (fail-closed) -> raw-log -> ACK,
#     BEFORE any vendor-specific code runs. telephony/handler.py's four triggers delegate to it
#     via the local `_receive` wrapper; whatsapp/webhook.py calls `spine.receive` directly.
#   rate_limit           — throttle marker on every webhook (a shared-tenant firehose); bounds
#     abuse volume. Present alongside the spine call on all 5 webhook endpoints.
#   is_downloadable       — storage.api.download_file's file-privacy gate (A.15): raises
#     frappe.PermissionError for a private, non-downloadable File before ever serving it.
#   PermissionError        — the throw-based deny used by that same file gate.
#   _scoped                — taxonomy.lookups' shared anti-enumeration bound (2+ chars required,
#     capped rows, ALL scope values required or `[]`); used by all three lookup endpoints.
#   _grain_from_form        — taxonomy.lookups.hospital_query's grain resolver: derives the grain
#     SERVER-SIDE from the named, published CRM Intake Form — a client-supplied grain value is
#     never read (A.9/A.16).
#   _force_published        — access.native_guards' LMS wrappers (get_courses/get_batches): forces
#     `published=1` for a non-privileged caller (metamorphic narrow), so an unauthenticated/no-LMS
#     caller can never enumerate DRAFT catalog rows via a crafted filter (VAPT Jun'26, Mode 2).
#   _lms_privileged         — the same module's LMS privilege check (get_job_details): strips the
#     creator email (`owner`) for a non-privileged caller. A real per-caller narrowing gate.
#   _insights_privileged   — native_guards.run_doc_method: strips the pipeline-rewind arg when the caller cannot READ the target, so Insights' permissions-off public path cannot replay a query before its own filters. Named, not `has_permission`, which returns an IGNORABLE boolean.
#   _published_course_from_referer — learning.outline's course recovery: the shim reads the course
#     from the Referer, which is CLIENT-SUPPLIED and so forgeable, and therefore honours it only for
#     a course the caller could already reach — published only for a non-privileged one (it reuses
#     _lms_privileged), the same bound _force_published puts on the catalog. A crafted Referer
#     cannot read a DRAFT course's outline.
GUEST_GATE_TOKENS = (
	"spine.receive",
	"_receive",
	"rate_limit",
	"is_downloadable",
	"PermissionError",
	"_scoped",
	"_grain_from_form",
	"_force_published",
	"_lms_privileged",
	"_insights_privileged",
	"_published_course_from_referer",
)


def _is_allow_guest_whitelist(decorator):
	"""True iff `decorator` is `@frappe.whitelist(allow_guest=True)` (or `...allow_guest = True`)."""
	if not isinstance(decorator, ast.Call) or not is_whitelist_decorator(decorator):
		return False
	for kw in decorator.keywords:
		if kw.arg == "allow_guest" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
			return True
	return False


def _guest_decorator(node):
	"""The `allow_guest=True` decorator Call on function `node`, or None."""
	for d in node.decorator_list:
		if _is_allow_guest_whitelist(d):
			return d
	return None


def _scan_guest_endpoints():
	"""Walk our app source and yield (dotted_name, func_node, decorator_node, source_lines) for
	every allow_guest whitelisted function — the raw material both assertions read from."""
	found = []
	for dirpath, dirnames, filenames in os.walk(_APP_ROOT):
		dirnames[:] = [d for d in dirnames if d not in ("tests", "__pycache__", "public", "node_modules")]
		for fn in filenames:
			if not fn.endswith(".py"):
				continue
			path = os.path.join(dirpath, fn)
			rel = os.path.relpath(path, _APP_ROOT)[:-3].replace(os.sep, ".")  # storage/api.py -> storage.api
			module = "tatva_connect." + rel
			with open(path, encoding="utf-8") as fh:
				src = fh.read()
			lines = src.splitlines()
			tree = ast.parse(src, filename=path)
			for node in ast.walk(tree):
				if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
					deco = _guest_decorator(node)
					if deco is not None:
						found.append((f"{module}.{node.name}", node, deco, lines))
	return found


def _has_marker(decorator, lines):
	"""True iff `# guest-ok:` appears on the decorator's own raw source line span — a comment,
	so it can only be found in raw text (never the AST)."""
	return MARKER in span_text(lines, decorator.lineno, decorator.end_lineno)


def _has_gate(node):
	"""True iff the function's own CODE (never a docstring/comment) contains a recognised
	self-gate token."""
	text = guard_text(node)
	return any(tok in text for tok in GUEST_GATE_TOKENS)


class TestGuestEndpoints(FrappeTestCase):
	def test_every_guest_endpoint_is_marked_and_gated(self):
		"""The dangerous direction: a public endpoint with no recorded reason, or no real gate,
		is exposed to an UNAUTHENTICATED caller on the strength of nothing but the decorator."""
		unmarked, ungated = [], []
		for name, node, deco, lines in _scan_guest_endpoints():
			if not _has_marker(deco, lines):
				unmarked.append(name)
			if not _has_gate(node):
				ungated.append(name)
		if unmarked:
			self.fail(
				f"PUBLIC EXPOSURE: {len(unmarked)} @frappe.whitelist(allow_guest=True) endpoint(s) have no "
				f"`# {MARKER} <reason>` marker on their decorator — a human must review WHY this is public "
				f"and record it there, or remove allow_guest: {sorted(unmarked)}"
			)
		if ungated:
			self.fail(
				f"PUBLIC EXPOSURE: {len(ungated)} @frappe.whitelist(allow_guest=True) endpoint(s) carry a "
				f"`# {MARKER}` marker but no recognised self-gate token in their own code — a marker alone "
				f"is not a gate. Add a real gate (token/signature auth, has_permission, a scope-bounding "
				f"helper) and, if it isn't in GUEST_GATE_TOKENS yet, add the helper's name there with a "
				f"comment; or remove allow_guest: {sorted(ungated)}"
			)

	def test_no_guest_endpoint_vanished_silently(self):
		"""Hygiene: the scan itself must find our known public surface — an empty result means the
		walker broke (wrong root, over-eager skip-dir), not that the app went private."""
		current = {name for name, *_ in _scan_guest_endpoints()}
		self.assertTrue(current, "no @frappe.whitelist(allow_guest=True) endpoints found at all — the scanner is broken")

	# ------------------------------------------------------------------
	# Self-tests (mirror test_no_perm_bypass.py's planted-bads — S.6: prove the lock isn't blind).
	# ------------------------------------------------------------------
	def _endpoint(self, src):
		"""Parse `src`, return (func_node, decorator_node, lines) for its one guest endpoint."""
		lines = src.splitlines()
		tree = ast.parse(src)
		func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
		deco = _guest_decorator(func)
		return func, deco, lines

	def test_missing_marker_is_caught(self):
		# (i) allow_guest, a real gate, but NO `# guest-ok:` marker -> marker check fails.
		func, deco, lines = self._endpoint(
			"import frappe\n"
			"@frappe.whitelist(allow_guest=True)\n"
			"@rate_limit(key='ip', limit=10, seconds=60)\n"
			"def leak():\n"
			"    return []\n"
		)
		self.assertIsNotNone(deco)
		self.assertFalse(_has_marker(deco, lines))
		self.assertTrue(_has_gate(func))

	def test_missing_gate_is_caught(self):
		# (ii) marker present, but the body has NO recognised gate token -> gate check fails.
		func, deco, lines = self._endpoint(
			"import frappe\n"
			"@frappe.whitelist(allow_guest=True)  # guest-ok: test fixture\n"
			"def leak():\n"
			"    return frappe.get_all('CRM Lead')\n"
		)
		self.assertTrue(_has_marker(deco, lines))
		self.assertFalse(_has_gate(func))

	def test_marker_and_gate_both_present_passes(self):
		# (iii) both present -> both checks pass.
		func, deco, lines = self._endpoint(
			"import frappe\n"
			"@frappe.whitelist(allow_guest=True)  # guest-ok: test fixture, rate-limited lookup\n"
			"@rate_limit(key='ip', limit=10, seconds=60)\n"
			"def public_lookup():\n"
			"    return []\n"
		)
		self.assertTrue(_has_marker(deco, lines))
		self.assertTrue(_has_gate(func))

	def test_docstring_only_gate_token_does_not_clear(self):
		# (iv) a gate-token word appearing ONLY in a docstring/comment must NOT clear the
		# function — guard_text reads code, not prose (mirrors the perm-bypass lock's T2/T3).
		func, deco, lines = self._endpoint(
			"import frappe\n"
			"@frappe.whitelist(allow_guest=True)  # guest-ok: test fixture\n"
			"def leak():\n"
			'    "calls rate_limit and is_downloadable internally"  # PermissionError note\n'
			"    return frappe.get_all('CRM Lead')\n"
		)
		self.assertTrue(_has_marker(deco, lines))
		self.assertFalse(_has_gate(func))
