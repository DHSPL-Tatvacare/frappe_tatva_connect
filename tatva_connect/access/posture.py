# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The request POSTURE — the ONE answer to "must this caller prove permission, and does it?".

Sibling of `entitlement.py` (which FIELDS) and `visibility.py` (which RECORDS). Those two narrow what a
principal may see. This one answers the question asked BEFORE either of them: is this request being made
by a principal at all, or by the server on behalf of one it has already gated?

THE MODEL. There are exactly two postures, and nothing in this app invents a third.

  * ORDINARY — a logged-in Desk/SPA user. Every check is put to the permission engine, unchanged. This is
    the default and it is what every request is unless a server has deliberately said otherwise.
  * TRUSTED — a block the SERVER opened around a caller it has already authorized on its own terms. The
    partner API is the only such caller today: it is a role-less user by design (`Partner API User` grants
    nothing), so it fails every native role check, and it is authorized instead by its enabled
    `CRM Lead API Mapping` and that mapping's grain. `api/_base.trusted_permissions()` opens the block;
    this module reads it.

THE FLAG IS SERVER-SET AND NEVER CALLER-SET. `frappe.flags` is per-request state on `frappe.local`; it is
not populated from the request body, the query string, or a whitelisted method's arguments. The only
writer in this app is the `trusted_permissions()` context manager, which restores the previous value on
exit — so the posture cannot leak past the block that opened it, and a caller cannot ask for it.

WHY THIS EXISTS AS A SEAM AND NOT AS A LINE OF CODE. `if not frappe.flags.ignore_permissions:` in front of
a `has_permission` call is one rule written per call site, and a rule written per call site is a rule that
gets forgotten. It was: the activity brain asked the posture at three of its nine checks, and the six that
never asked were simply the six no partner had happened to reach yet. A partner creating an activity whose
type declares a `source = Lead` field reached one, met a bare `PermissionError` with no message, and got a
403 that recorded no reason. Adding a tenth guard would have set the same trap for the eleventh.

So: nothing outside this module decides the posture, and nothing outside it decides what a trusted caller
skips. A consumer asks `require(...)` and is correct by construction.
`tests/architecture/test_permission_checks_one_seam.py` fails if a bare `frappe.has_permission` grows back
in a module that has been brought onto the seam.

WHAT TRUSTED DOES NOT BUY. Only the engine's ROLE/row check. The field-level gates still run in full: a
partner's `entitled_grains` resolves to its own contract grain, so `entitlement.resolve_fields` shows it
exactly the fields its contract ticks and no others, trusted or not. Bypassing the role check on a caller
the mapping already scoped is not the same as showing it somebody else's data, and this module widens
nothing beyond the first.
"""
import frappe


def is_trusted():
	"""Is this request running in the trusted posture — i.e. inside a server-opened, pre-gated block?

	THE one reader of the flag. Callers that must pass the posture on to the framework (`doc.save`,
	`doc.insert`) read it here too, so "trusted" means one thing on the gate and on the write alike."""
	return bool(frappe.flags.ignore_permissions)


def require(doctype, ptype, doc=None):
	"""Assert the caller may `ptype` this record, unless the server has already vouched for them.

	THE one permission checkpoint for every module brought onto this seam. Ordinary posture puts the
	question to the permission engine verbatim — same doctype, same ptype, same doc, same throw — so a
	Desk request is answered exactly as it was before the seam existed. Trusted posture returns without
	asking, because the check would be put to a role-less user that the mapping and grain gate already
	admitted, and its only possible answer is a refusal with nothing to say.

	`doc` is the record when there is one, and None for a doctype-level question; both are passed through
	untouched, because deciding which of the two applies is the CALLER's knowledge and re-deciding it here
	would be a second brain over the framework's own signature."""
	if is_trusted():
		return
	frappe.has_permission(doctype, ptype, doc=doc, throw=True)
