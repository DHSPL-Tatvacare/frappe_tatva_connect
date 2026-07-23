# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""CaseSpec + the curated Tier-2 case list.

Each CaseSpec is one concrete (attack × principal × target × surface × action) point with a stable
id, an English description, the expected verdict (allow/deny), and the oracle to judge against
(defaults to the attack's oracle, overridable per case). The runner (test_registry_cases.py)
iterates these; the report and confusion matrix key on the id.

This is the SEED set covering the audit-critical vectors (permlevel field leak, the #4/#5
same-program trap, grain-vs-role, horizontal leak, cross-app deny, bypass-write). Expand by adding
rows — every attack key in attacks.py should end with >=1 case and >=1 planted mutation.
"""
from dataclasses import dataclass

from tatva_connect.tests.authz.registry.attacks import ATTACKS
from tatva_connect.tests.authz.registry.endpoints import APP_ENDPOINTS, GENERIC_ENDPOINTS, SENSITIVE_DOCTYPES

# The API-layer deny-sweep runs as principals that hold NO business access to the sensitive doctypes:
# the strict floor (no_role), the real signup (default_user), the anonymous caller (guest), and the
# cross-app junk role. The oracle confirms each is natively denied; a case that gets through is an
# escalation (the VAPT class). Grain roles are covered by the in-process A1..A14 cases, not here.
HOSTILE_PRINCIPALS = ["no_role", "default_user", "guest", "junk_crossapp"]


@dataclass(frozen=True)
class CaseSpec:
	id: str
	attack: str            # key into ATTACKS
	english: str           # human description of what this case proves
	principal: str         # roster persona name (roster.PERSONAS[].persona) or a role string
	doctype: str
	action: str            # read | write | create | delete | field_read | list | info
	surface: str           # list | doc | field | bypass_write | method | http
	target: str            # in_grain | out_of_grain | ... | other_owner | na
	expected: str          # "allow" | "deny"
	oracle: str = ""       # override; defaults to the attack's oracle
	engine: str = "in_process"  # in_process | http
	method: str = ""       # http only: the dotted whitelisted path
	http: str = ""         # http only: GET | POST
	endpoint_key: str = ""  # http only: key into endpoints.{GENERIC,APP}_ENDPOINTS (rebuilds params)

	def resolved_oracle(self):
		return self.oracle or ATTACKS[self.attack]["oracle"]


def generate_http_cases():
	"""PRODUCE the API-layer (B1..B5) cases by crossing the endpoint primitives with the hostile
	principals — never hand-listed. Doctype-parametric generics cross SENSITIVE_DOCTYPES; named app
	methods cross principals on their own fixed doctype. Every case expects DENY (the oracle confirms)."""
	out = []
	for principal in HOSTILE_PRINCIPALS:
		for ep in GENERIC_ENDPOINTS:
			for meta in SENSITIVE_DOCTYPES:
				dt = meta["doctype"]
				# a READ on a PRIVATE doctype (File) IS the B5 private-file surface per ATTACKS, not the
				# generic B1 read — same endpoint, but the vector the case proves is the file-IDOR class.
				vector = "B5" if (ep.action == "read" and meta.get("private")) else ep.vector
				out.append(CaseSpec(
					id=f"{vector}-{principal}-{ep.key}-{dt.replace(' ', '')}",
					attack=vector, principal=principal, doctype=dt, action=ep.action,
					surface="http", target="other_owner", expected="deny",
					english=f"{principal} calling {ep.method} on a foreign-owned {dt} must be denied ({vector})",
					engine="http", method=ep.method, http=ep.http, endpoint_key=ep.key))
		for ep in APP_ENDPOINTS:
			dt = ep.doctype or "na"
			out.append(CaseSpec(
				id=f"{ep.vector}-{principal}-{ep.key}",
				attack=ep.vector, principal=principal, doctype=ep.doctype, action=ep.action,
				surface="http", target="other_owner" if ep.doctype else "na", expected="deny",
				english=f"{principal} calling {ep.method} must be denied ({ep.vector})",
				engine="http", method=ep.method, http=ep.http, endpoint_key=ep.key))
	return out


# Curated seed cases. ids are stable: <attack>-<principal>-<doctype-short>-<action>-<surface>.
CASES = [
	# A1 — horizontal grain leak: a grain user must see only their own grain's leads.
	CaseSpec("A1-grain4-lead-read-list", "A1",
	         "grain_4 (Tatvapractice/India/Inside-Sales) lists CRM Lead — sees only its grain's leads",
	         "grain_4", "CRM Lead", "read", "list", "in_grain", "allow"),
	CaseSpec("A1-grain4-othergrain-read-list", "A1",
	         "grain_4 must NOT see grain_1 (Goodflip-Care/Anaya) leads in the list",
	         "grain_4", "CRM Lead", "read", "list", "out_of_grain", "deny"),

	# A2 — THE trap: #4 and #5 share program 'Inside-Sales' but differ on vertical+group.
	CaseSpec("A2-grain4-grain5-read-list", "A2",
	         "grain_4 must NOT see grain_5 (Goodflip/B2C/Inside-Sales) leads despite the shared "
	         "program name 'Inside-Sales' — matching keys on ALL axes, never program alone",
	         "grain_4", "CRM Lead", "read", "list", "same_program_diff_vertical", "deny"),

	# A4 — grain-vs-role contradiction: a CRM Lead Field Restriction must win over grain visibility.
	CaseSpec("A4-grain1-restricted-field", "A4",
	         "a field hidden from grain_1's role by CRM Lead Field Restriction stays hidden even "
	         "though the grain would otherwise show it",
	         "grain_1", "CRM Lead", "field_read", "field", "in_grain", "deny"),

	# A7 — grain fields are READ-allowed but EDIT-denied for a grain user. The intended model (confirmed
	# 2026-06-29): a Sales User SEES which grain a lead belongs to, but only a manager / the assignment-
	# rule stage may MOVE it. vertical/group are permlevel-1 (structurally unwritable by a Sales User);
	# current_program is permlevel-0 but the grain controller rejects an out-of-entitlement save when the
	# grain switch is ON (switch OFF = the documented stock exposure, like the child-visibility switches).
	# The field READ-leak to a principal WITHOUT permlevel-1 read is the negative control proven in
	# mutation.py (a fresh permlevel-0 role), not a case here — no roster persona has Lead read yet lacks
	# permlevel-1 read.
	CaseSpec("A7-grain1-reads-own-grain-fields", "A7",
	         "grain_1 (Sales User) CAN read its own lead's grain fields (vertical/group/program) — "
	         "intended, NOT a leak; the protection is on EDIT, not read",
	         "grain_1", "CRM Lead", "field_read", "field", "in_grain", "allow"),
	CaseSpec("A7-grain1-cannot-move-lead-out-of-grain", "A7",
	         "grain_1 CANNOT move a lead to a program outside its entitlement (grain edit-denied; only a "
	         "manager / the assignment-rule stage may change grain) — enforced when the grain switch is ON",
	         "grain_1", "CRM Lead", "write", "field", "in_grain", "deny"),

	# A6 — bypass-write escalation: partner mapped to grain_1 must not write an out-of-grain lead.
	CaseSpec("A6-partner-out-of-grain-write", "A6",
	         "partner (mapped to grain_1) writing a Tatvapractice lead is rejected BEFORE the "
	         "ignore_permissions save (partner out-of-grain raises DoesNotExistError)",
	         "partner", "CRM Lead", "write", "bypass_write", "out_of_grain", "deny"),

	# A11 — cross-app leak: the junk cross-app role must not reach CRM doctypes (the old VAPT class).
	CaseSpec("A11-junk-contact-read", "A11",
	         "junk_crossapp (Purchase Master Manager) must NOT read Contact (the regression the "
	         "original VAPT suite caught)",
	         "junk_crossapp", "Contact", "read", "doc", "na", "deny"),

	# A3 — vertical escalation: no_role must not gain any CRM Lead capability.
	CaseSpec("A3-norole-lead-write", "A3",
	         "no_role user has no CRM Lead write capability (baseline escalation guard)",
	         "no_role", "CRM Lead", "write", "doc", "na", "deny"),

	# A5 — reports_to roll-up: a manager's entitled grains must be ⊆ the union of reports' grains;
	# never wider than the manager's own native row visibility. (Vacuous on a bench without the
	# reports_to column — the mutation/self-validation declares that explicitly, never silent.)
	CaseSpec("A5-salesmgr-rollup-bounded", "A5",
	         "sales_manager's rolled-up grain visibility must not exceed the union of its reports' "
	         "grains, and never widen beyond native row visibility",
	         "sales_manager", "CRM Lead", "read", "list", "out_of_grain", "deny"),

	# A8 — orphan / forged parent: a child whose parent ref is missing/forged must be invisible.
	CaseSpec("A8-grain1-forged-parent-task", "A8",
	         "grain_1 must NOT read a CRM Task whose reference_docname is forged/orphaned "
	         "(fail-closed: no parent => not visible)",
	         "grain_1", "CRM Task", "read", "doc", "orphan_parent", "deny"),

	# A9 — child-doctype visibility (the ROW-visibility brain, access/visibility.py). Each child
	# inherits its parent Lead/Deal scope. Consolidated from the four superseded child-scope suites
	# (notes/tasks/telephony/whatsapp test_*_scope.py): per doctype we prove (a) switch ON blocks the
	# out-of-scope peer AND keeps the in-scope owner (child_scope), (b) switch OFF = stock CRM exposure,
	# the documented delta (child_off), and (c) the reference_*/owner/assigned_to PQC inheritance shape
	# (child_shape). Two carve-outs keep their unique bits: a links-only Call Log fails closed, and a
	# task assignee sees their own task on a hidden parent (least-privilege). Owner=grain_1, peer=grain_2,
	# child planted on a grain_1 lead; the handler toggles the switch in a savepoint (base.py discipline).
	CaseSpec("A9-task-switch-on-peer-blocked", "A9",
	         "Task::CRM Task::visibility ON: the out-of-scope peer (grain_2) cannot read a CRM Task on a "
	         "grain_1 lead it cannot see, while the in-scope owner (grain_1) can",
	         "grain_2", "CRM Task", "read", "child_scope", "out_of_grain", "deny"),
	CaseSpec("A9-task-switch-off-stock-exposure", "A9",
	         "with the CRM Task visibility switch OFF, scoped_pqc is empty and the peer CAN read the task "
	         "(stock CRM, no child scoping) — the documented switch-OFF exposure delta",
	         "grain_2", "CRM Task", "read", "child_off", "out_of_grain", "allow"),
	CaseSpec("A9-task-inheritance-shape", "A9",
	         "CRM Task list PQC inherits parent scope via owner + reference_doctype + reference_docname, "
	         "and (uniquely among the four) an assigned_to self-ownership clause",
	         "grain_2", "CRM Task", "read", "child_shape", "out_of_grain", "deny"),
	CaseSpec("A9-callog-switch-on-peer-blocked", "A9",
	         "Telephony::CRM Call Log::visibility ON: the peer cannot read a Call Log referencing a "
	         "grain_1 lead it cannot see, while the in-scope owner can",
	         "grain_2", "CRM Call Log", "read", "child_scope", "out_of_grain", "deny"),
	CaseSpec("A9-callog-switch-off-stock-exposure", "A9",
	         "with the Call Log visibility switch OFF, scoped_pqc is empty and the peer CAN read the call "
	         "log (stock CRM) — the documented switch-OFF exposure delta",
	         "grain_2", "CRM Call Log", "read", "child_off", "out_of_grain", "allow"),
	CaseSpec("A9-callog-inheritance-shape", "A9",
	         "CRM Call Log list PQC inherits parent scope via owner + reference_doctype + "
	         "reference_docname, with NO assigned_to clause (Call Log has no such column)",
	         "grain_2", "CRM Call Log", "read", "child_shape", "out_of_grain", "deny"),
	CaseSpec("A9-callog-links-only-fail-closed", "A9",
	         "a links-only CRM Call Log (no reference_* parent) is an orphan -> denied even for the "
	         "in-scope user (fail-closed; the single-doc gate and reference-only PQC agree)",
	         "grain_1", "CRM Call Log", "read", "child_orphan", "in_grain", "deny"),
	CaseSpec("A9-note-switch-on-peer-blocked", "A9",
	         "Note::FCRM Note::visibility ON: the peer cannot read an FCRM Note on a grain_1 lead it "
	         "cannot see, while the in-scope owner can",
	         "grain_2", "FCRM Note", "read", "child_scope", "out_of_grain", "deny"),
	CaseSpec("A9-note-switch-off-stock-exposure", "A9",
	         "with the FCRM Note visibility switch OFF, scoped_pqc is empty and the peer CAN read the "
	         "note (stock CRM) — the documented switch-OFF exposure delta",
	         "grain_2", "FCRM Note", "read", "child_off", "out_of_grain", "allow"),
	CaseSpec("A9-note-inheritance-shape", "A9",
	         "FCRM Note list PQC inherits parent scope via owner + reference_doctype + reference_docname, "
	         "with NO assigned_to clause",
	         "grain_2", "FCRM Note", "read", "child_shape", "out_of_grain", "deny"),
	CaseSpec("A9-whatsapp-switch-on-peer-blocked", "A9",
	         "WhatsApp::WhatsApp Message::visibility ON: the peer cannot read a WhatsApp Message on a "
	         "grain_1 lead it cannot see, while the in-scope owner can",
	         "grain_2", "WhatsApp Message", "read", "child_scope", "out_of_grain", "deny"),
	CaseSpec("A9-whatsapp-switch-off-stock-exposure", "A9",
	         "with the WhatsApp Message visibility switch OFF, scoped_pqc is empty and the peer CAN read "
	         "the message (stock CRM) — the documented switch-OFF exposure delta",
	         "grain_2", "WhatsApp Message", "read", "child_off", "out_of_grain", "allow"),
	CaseSpec("A9-whatsapp-inheritance-shape", "A9",
	         "WhatsApp Message list PQC inherits parent scope via owner + reference_doctype + "
	         "reference_name (NOT reference_docname — the field name differs), with NO assigned_to clause",
	         "grain_2", "WhatsApp Message", "read", "child_shape", "out_of_grain", "deny"),
	CaseSpec("A9-task-assignee-sees-own-on-hidden-parent", "A9",
	         "least-privilege carve-out: a CRM Task ASSIGNED to grain_2 is visible to grain_2 even on a "
	         "grain_1 parent it cannot see (assigning a task grants the task, not the lead)",
	         "grain_2", "CRM Task", "read", "child_assignee", "out_of_grain", "allow"),

	# A10 — native-method bypass: the 11 engine-bypassing native crm methods wrapped by
	# access.native_guards (registered in hooks.py under override_whitelisted_methods). Consolidated from
	# the L3 method-gate layer of the superseded test_vapt_authz.py — the only RUNTIME exercise of the
	# wrappers. Three angles, driven through the REAL override dispatch: (1) a PEER calling a row-scoped
	# guard on a row it cannot read is denied; (2) a NO-ROLE caller is denied at the doctype matrix
	# (privilege escalation); (3) the AUTHORIZED owner is NOT denied (no regression). CaseSpec.method
	# carries the native dotted path; the handler resolves the override and asserts PermissionError.
	CaseSpec("A10-peer-callog-get-recording-url", "A10",
	         "a peer (grain_2) calling get_recording_url on a CRM Call Log referencing a lead it cannot "
	         "read is denied (the guard _require_read throws before the native call)",
	         "grain_2", "CRM Call Log", "read", "method", "out_of_grain", "deny",
	         method="crm.integrations.api.get_recording_url"),
	CaseSpec("A10-peer-callog-add-task", "A10",
	         "a peer calling add_task_to_call_log on a Call Log it cannot read is denied",
	         "grain_2", "CRM Call Log", "write", "method", "out_of_grain", "deny",
	         method="crm.integrations.api.add_task_to_call_log"),
	CaseSpec("A10-peer-callog-add-note", "A10",
	         "a peer calling add_note_to_call_log on a Call Log it cannot read is denied",
	         "grain_2", "CRM Call Log", "write", "method", "out_of_grain", "deny",
	         method="crm.integrations.api.add_note_to_call_log"),
	CaseSpec("A10-peer-lead-assigned-users", "A10",
	         "a peer calling get_assigned_users on an out-of-grain CRM Lead it cannot read is denied",
	         "grain_2", "CRM Lead", "read", "method", "out_of_grain", "deny",
	         method="crm.api.doc.get_assigned_users"),
	CaseSpec("A10-peer-lead-linked-docs", "A10",
	         "a peer calling get_linked_docs_of_document on an out-of-grain CRM Lead it cannot read is "
	         "denied",
	         "grain_2", "CRM Lead", "read", "method", "out_of_grain", "deny",
	         method="crm.api.doc.get_linked_docs_of_document"),
	CaseSpec("A10-peer-lead-whatsapp-messages", "A10",
	         "a peer calling get_whatsapp_messages on an out-of-grain CRM Lead it cannot read is denied",
	         "grain_2", "CRM Lead", "read", "method", "out_of_grain", "deny",
	         method="crm.api.whatsapp.get_whatsapp_messages"),
	CaseSpec("A10-norole-callog-get-recording-url", "A10",
	         "a no-role caller is denied get_recording_url at the doctype matrix (privilege escalation) — "
	         "no CRM Call Log read capability at all",
	         "no_role", "CRM Call Log", "read", "method", "out_of_grain", "deny",
	         method="crm.integrations.api.get_recording_url"),
	CaseSpec("A10-norole-lead-assigned-users", "A10",
	         "a no-role caller is denied get_assigned_users on a CRM Lead (privilege escalation)",
	         "no_role", "CRM Lead", "read", "method", "out_of_grain", "deny",
	         method="crm.api.doc.get_assigned_users"),
	CaseSpec("A10-authorized-lead-assigned-users", "A10",
	         "the AUTHORIZED owner (grain_1) is NOT denied get_assigned_users on its own in-grain lead — "
	         "the negative control proving the guard gates escalation, not every caller",
	         "grain_1", "CRM Lead", "read", "method", "in_grain", "allow",
	         method="crm.api.doc.get_assigned_users"),

	# A12 — User Permission / DocShare over-grant: a fenced user sees only fenced rows; a share
	# grants only what was explicitly shared, never more.
	CaseSpec("A12-grain1-fenced-overreach", "A12",
	         "a User-Permission-fenced grain_1 must NOT see rows outside the fence, and an explicit "
	         "DocShare grants only the shared row",
	         "grain_1", "CRM Lead", "read", "list", "out_of_grain", "deny"),

	# A13 — partner-mapping abuse: a DISABLED mapping must deny the partner entirely (no leak).
	CaseSpec("A13-partner-disabled-mapping", "A13",
	         "with its CRM Lead API Mapping disabled, the partner is denied all access (no "
	         "wrong-tenant attribution, invariant 16)",
	         "partner", "CRM Lead", "read", "bypass_write", "in_grain", "deny"),
	# A13 — partner CALL/ACTIVITY create against an OUT-OF-GRAIN lead: resolve_lead's forced grain
	# filter raises (DoesNotExistError) BEFORE the ignore_permissions save, so the partner (grain_1)
	# can never plant a child on a grain_3 lead.
	CaseSpec("A13-partner-callog-out-of-grain-create", "A13",
	         "partner (grain_1) creating a CRM Call Log against a grain_3 lead is rejected by "
	         "resolve_lead's forced grain filter before the ignore_permissions save",
	         "partner", "CRM Call Log", "create", "bypass_write", "out_of_grain", "deny"),
	CaseSpec("A13-partner-activity-out-of-grain-create", "A13",
	         "partner (grain_1) creating a CRM Task (activity) against a grain_3 lead is rejected by "
	         "resolve_lead's forced grain filter before the ignore_permissions save",
	         "partner", "CRM Task", "create", "bypass_write", "out_of_grain", "deny"),
	# A13 — external_id is a LABEL, never an address: NOTHING in the partner API resolves by it, so a
	# colliding external_id already on a grain_3 lead's row is inert. Sending it creates the caller's
	# OWN row and leaves the other tenant's row untouched.
	CaseSpec("A13-partner-extid-collision-no-cross-tenant", "A13",
	         "partner (grain_1) sending an external_id that already exists on a grain_3 lead's CRM "
	         "Call Log creates a NEW row on their own lead and leaves the grain_3 row byte-for-byte "
	         "untouched — external_id never resolves a record, so a colliding label cannot reach "
	         "another tenant",
	         "partner", "CRM Call Log", "create", "bypass_write", "out_of_grain", "deny"),

	# A14 — public-intake guest abuse: an anonymous web-form submit must not escape the form's grain.
	CaseSpec("A14-guest-routing-forced", "A14",
	         "a Guest submission carrying foreign custom_vertical/group/program lands on the FORM's "
	         "grain (forced routing), never the smuggled one — is_sysmgr=False+mp drops submitter routing",
	         "Guest", "CRM Lead", "write", "bypass_write", "out_of_grain", "deny"),
	CaseSpec("A14-guest-no-master-growth", "A14",
	         "a Guest manual-field submit into a GROWABLE master (CRM Side Effect Option — not "
	         "pick-only, not grain-scoped) returns canonical text and grows NO row, while the SAME "
	         "_ensure_master call as an authed user grows it by 1 — the differential proves the block "
	         "is the Guest guard (intake.py:355), not a universal/pick-only one",
	         "Guest", "CRM Side Effect Option", "create", "bypass_write", "na", "deny"),
	CaseSpec("A14-guest-note-scope", "A14",
	         "a Guest fold writes its FCRM Note ONLY against the form's own resolved lead "
	         "(reference_docname == the fold's lead) — never another lead",
	         "Guest", "FCRM Note", "create", "bypass_write", "in_grain", "allow"),

	# A7 (Smart View column leak): a grain user authoring a Smart View cannot project a column outside
	# the view's grain catalog — _validate_columns is a fail-closed allowlist (any non-catalog key throws).
	CaseSpec("A7-grain1-smartview-out-of-grain-column", "A7",
	         "grain_1 (Sales User) calling upsert_view with a column that is not in its grain's catalog "
	         "is rejected by _validate_columns (fail-closed allowlist) before the view is saved",
	         "grain_1", "CRM Smart View", "create", "smartview", "out_of_grain", "deny"),

	# A12 (Smart View grain clamp): a grain user cannot author a view scoped to a grain it isn't entitled
	# to — _grains_from_axes clamps explicit axes to entitlement and raises PermissionError fail-closed.
	CaseSpec("A12-grain1-smartview-grain-clamp", "A12",
	         "grain_1 (Sales User) calling upsert_view with an out-of-grain (vertical,group,program) "
	         "axis is rejected by _grains_from_axes (the cross-tenant write clamp) with PermissionError",
	         "grain_1", "CRM Smart View", "create", "smartview", "out_of_grain", "deny"),
]


def all_cases():
	return list(CASES)


def cases_for(attack_key):
	return [c for c in CASES if c.attack == attack_key]


def attacks_without_cases():
	"""Attack keys that have no case yet — surfaced so coverage gaps are visible, never silent. The
	B1..B5 endpoint vectors are covered by the GENERATED http sweep (never hand-listed in CASES), so
	their generated cases count toward coverage exactly like the curated A1..A14 rows."""
	covered = {c.attack for c in CASES} | {c.attack for c in generate_http_cases()}
	return [k for k in ATTACKS if k not in covered]
