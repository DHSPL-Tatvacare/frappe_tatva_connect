# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The deterministic dummy-user roster — ALWAYS provisioned by generator.seed() so every persona's
login is ready for Playwright (creds.json). Emails are namespaced `authz.*@example.test` so the
teardown is unambiguous. Roles are REAL roles confirmed on the site (recon): Sales User,
Sales Manager, WhatsApp User, WhatsApp Admin.
"""
from tatva_connect.tests.authz.grains import GRAINS

PASSWORD = "AuthzTest!2026"  # dev-only fixed password; creds.json is gitignored, never on prod
DOMAIN = "example.test"


def _email(slug):
	return "authz.{0}@{1}".format(slug, DOMAIN)


# One Sales User per grain — the primary horizontal-isolation principals.
_grain_personas = [
	{
		"persona": "grain_{0}".format(i + 1),
		"email": _email("grain{0}".format(i + 1)),
		"password": PASSWORD,
		"roles": ["Sales User"],
		"grain_key": g["key"],
	}
	for i, g in enumerate(GRAINS)
]

# Role-only / no-grain personas — for cross-role, escalation, and catastrophe checks.
_role_personas = [
	{"persona": "sales_manager", "email": _email("salesmgr"), "password": PASSWORD,
	 "roles": ["Sales Manager"], "grain_key": None},
	{"persona": "whatsapp_user", "email": _email("wauser"), "password": PASSWORD,
	 "roles": ["WhatsApp User"], "grain_key": None},
	{"persona": "whatsapp_admin", "email": _email("waadmin"), "password": PASSWORD,
	 "roles": ["WhatsApp Admin"], "grain_key": None},
	# role-combo: probes vertical escalation (does holding two roles leak either's perms wrongly?)
	{"persona": "combo_sales_wa", "email": _email("combo"), "password": PASSWORD,
	 "roles": ["Sales User", "WhatsApp User"], "grain_key": None},
	# partner: NO desk role — grain comes from CRM Lead API Mapping, set in generator
	{"persona": "partner", "email": _email("partner"), "password": PASSWORD,
	 "roles": [], "grain_key": None},
	# no-role authenticated user: the catastrophe baseline (must be denied almost everything)
	{"persona": "no_role", "email": _email("norole"), "password": PASSWORD,
	 "roles": [], "grain_key": None},
	# cross-app junk role: the existing VAPT suite used "Purchase Master Manager" because it
	# reproduced "reads all Contacts". Keep a cross-app principal so that regression class (A11/A3)
	# stays covered. The Tier-1 sweep additionally enumerates ALL roles, not just this one.
	{"persona": "junk_crossapp", "email": _email("junk"), "password": PASSWORD,
	 "roles": ["Purchase Master Manager"], "grain_key": None},
]

PERSONAS = _grain_personas + _role_personas


def by_persona(name):
	return next(p for p in PERSONAS if p["persona"] == name)


def email(persona):
	return by_persona(persona)["email"]
