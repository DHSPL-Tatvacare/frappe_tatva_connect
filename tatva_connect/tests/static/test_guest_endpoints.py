# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The public attack surface — every `@frappe.whitelist(allow_guest=True)` endpoint in our app.

An `allow_guest=True` whitelisted method is callable over HTTP by an UNAUTHENTICATED caller (the
internet). That is the single scariest thing to get wrong: one stray decorator exposes an endpoint to
the world. This test scans our source (AST — no import/DB state to fool it) for every such endpoint and
asserts the set EXACTLY matches a hand-REVIEWED allowlist. A new public endpoint fails the build until a
human reviews it and adds it here with a reason.

Scope note: this guards the SET of public endpoints (no accidental new one). Whether each public
endpoint is itself safe — verifies a provider token, never trusts a client-supplied grain (A.9/A.16),
gates private data — is asserted by the per-surface tests (webhook spine, telephony, storage privacy)
and the bypass-write audit. Here we hold the line on "nothing NEW is public".

Pure AST + stdlib: runs standalone under `tcsec locks` and inside `bench run-tests`.
"""
import ast
import os
import unittest


def _app_root():
	"""The tatva_connect app dir, found by walking up to the `hooks.py` marker — robust to where
	this test file sits (survives a tests/ reorg) and identical in the bench and standalone."""
	d = os.path.dirname(os.path.abspath(__file__))
	while d != os.path.dirname(d):
		if os.path.exists(os.path.join(d, "hooks.py")):
			return d
		d = os.path.dirname(d)
	raise RuntimeError("tatva_connect app root (hooks.py) not found above this test")


_APP_ROOT = _app_root()

# The REVIEWED public (allow_guest) endpoints — audit 2026-06-29. Each is public for a concrete reason
# and gates itself internally; adding to this list is a SECURITY REVIEW, not a formality.
REVIEWED_GUEST_ENDPOINTS = {
	# Provider webhooks — the telephony / WhatsApp providers POST here with no session. Each verifies a
	# shared token / DID inside before acting (constitution A.16: no best-guess inbound attribution).
	"tatva_connect.telephony.handler.inbound_answered",
	"tatva_connect.telephony.handler.inbound_complete",
	"tatva_connect.telephony.handler.outbound_answered",
	"tatva_connect.telephony.handler.outbound_complete",
	"tatva_connect.whatsapp.webhook.webhook",
	# Public file fetch — returns a short-lived SAS link; PRIVATE files are permission-gated inside
	# (constitution A.15 file privacy fail-closed).
	"tatva_connect.storage.api.download_file",
	# Public web-form autocompletes — read-only, server-scoped lookups; never trust a client grain
	# (constitution A.9). The picklist endpoints that DO scope by grain are deliberately NOT allow_guest.
	"tatva_connect.taxonomy.lookups.city_query",
	"tatva_connect.taxonomy.lookups.hospital_query",
	"tatva_connect.taxonomy.lookups.doctor_query",
}


def _is_allow_guest_whitelist(decorator):
	"""True iff `decorator` is `@frappe.whitelist(allow_guest=True)` (or `...allow_guest = True`)."""
	if not isinstance(decorator, ast.Call):
		return False
	func = decorator.func
	if not (isinstance(func, ast.Attribute) and func.attr == "whitelist"):
		return False
	for kw in decorator.keywords:
		if kw.arg == "allow_guest" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
			return True
	return False


def _scan_guest_endpoints():
	"""Walk our app source and return the set of dotted paths of every allow_guest whitelisted func."""
	app_root = _APP_ROOT  # .../apps/tatva_connect/tatva_connect
	found = set()
	for dirpath, dirnames, filenames in os.walk(app_root):
		dirnames[:] = [d for d in dirnames if d not in ("tests", "__pycache__", "public", "node_modules")]
		for fn in filenames:
			if not fn.endswith(".py"):
				continue
			path = os.path.join(dirpath, fn)
			rel = os.path.relpath(path, app_root)[:-3].replace(os.sep, ".")  # storage/api.py -> storage.api
			module = "tatva_connect." + rel
			with open(path, encoding="utf-8") as fh:
				tree = ast.parse(fh.read(), filename=path)
			for node in ast.walk(tree):
				if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
					if any(_is_allow_guest_whitelist(d) for d in node.decorator_list):
						found.add(f"{module}.{node.name}")
	return found


class TestGuestEndpoints(unittest.TestCase):
	def test_no_unreviewed_public_endpoint(self):
		"""The dangerous direction: a NEW allow_guest endpoint exposed to the internet without review."""
		current = _scan_guest_endpoints()
		unreviewed = sorted(current - REVIEWED_GUEST_ENDPOINTS)
		self.assertFalse(
			unreviewed,
			f"PUBLIC EXPOSURE: {len(unreviewed)} new @frappe.whitelist(allow_guest=True) endpoint(s) are callable by "
			"an UNAUTHENTICATED caller and have NOT been security-reviewed. Review each (does it verify "
			"a token? leak data? trust a client grain?), then add it to REVIEWED_GUEST_ENDPOINTS with a "
			f"reason — or remove allow_guest: {unreviewed}",
		)

	def test_reviewed_allowlist_not_stale(self):
		"""Hygiene: an entry in the allowlist that no longer exists means the audit list drifted."""
		current = _scan_guest_endpoints()
		stale = sorted(REVIEWED_GUEST_ENDPOINTS - current)
		self.assertFalse(
			stale,
			"the reviewed guest-endpoint allowlist names endpoint(s) that no longer exist — remove "
			f"them so the audit list stays accurate: {stale}",
		)
