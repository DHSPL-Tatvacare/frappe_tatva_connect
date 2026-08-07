# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A field whose stored value is EXECUTED may be written by an admin only, and a link is never a scheme.

Two lenses, because one alone has never been enough:

  * WHO MAY WRITE. A field whose contract is executable code cannot be sanitised (cleaning it destroys
    it) and cannot be contained by CSP (removing `unsafe-inline` breaks the desk). Authorship is the
    ONLY control that exists for it, so the sweep below asserts no non-admin role holds one — with a
    reviewed register of the exceptions, each carrying its reason.
  * WHAT MAY BE STORED. Everything else — a value that becomes a URL — is judged at write time,
    whoever wrote it, so a compromised admin account does not walk straight through.

The sweep is derived from the LIVE schema, not a typed list, so an app install or a version bump that
introduces a new executable field fails here instead of being discovered by an auditor. It is deliberately
NOT filtered by the field's declared language: that tag is spelled inconsistently across apps and is
absent on some fields, so the doctype is judged and the register carries the verdict.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.access.test_executable_fields
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access.link_scheme import is_safe_scheme
from tatva_connect.access.user_links import LINK_FIELDS

# Roles that ARE the administrator; a grant to one of these is not an escalation.
ADMIN_ROLES = {"Administrator", "System Manager"}

# Code fields whose declared language a browser or the server runs. Normalised — apps spell these
# inconsistently ("JS" vs "Javascript", "PythonExpression" vs "Python Expression").
EXEC_LANGUAGES = {"js", "javascript", "html", "css", "scss", "python", "pythonexpression", "jinja", "sql"}

# The REGISTER: a non-admin role that may hold an executable field, and why. An entry here is a standing
# decision, not an oversight — and anything NOT here fails the sweep.
REVIEWED_EXCEPTIONS = {
	"Server Script": {"Script Manager": "the role exists to author server scripts"},
	"Report": {"Report Manager": "a query report IS its script"},
	"Web Form": {"Website Manager": "owns the public site"},
	"Web Page": {"Website Manager": "owns the public site"},
	"Website Script": {"Website Manager": "owns the public site"},
	"Website Settings": {"Website Manager": "owns the public site"},
	"Website Theme": {"Website Manager": "owns the public site"},
	"Assignment Rule": {"Agent Manager": "assignment conditions are the feature"},
	"HD Form Script": {"Agent Manager": "helpdesk form scripting is the feature"},
	"HD Service Level Agreement": {"Agent Manager": "SLA conditions are the feature"},
	"CRM Form Script": {"Sales Manager": "CRM form scripting is the feature; live rows exist"},
	"CRM Service Level Agreement": {"Sales Manager": "SLA conditions are the feature"},
	"HD Ticket Template": {"Agent Manager": "template authoring is the manager's job; frontline Agent is read-only"},
	"WhatsApp Notification": {"Script Manager": "notification conditions are the feature"},
	"Insights Query": {"Insights User": "an analyst writing a query is the product"},
	"Insights Table v3": {"Insights Admin": "import scripting is the feature"},
	"LMS Programming Exercise Submission": {
		"Course Creator": "the exercise author writes the reference solution",
		"Batch Evaluator": "an evaluator reads and annotates a submission",
		"Moderator": "moderates submissions",
		"LMS Student": "a student's own answer to a programming exercise IS code, and it is if_owner",
	},
}


def _perm_source(doctype):
	"""Custom DocPerm OVERRIDES DocPerm entirely when any row exists — resolve the way frappe does."""
	return "Custom DocPerm" if frappe.db.exists("Custom DocPerm", {"parent": doctype}) else "DocPerm"


def _non_admin_authors(doctype):
	"""Roles other than the admin roles that may CREATE or WRITE `doctype` at permlevel 0."""
	rows = frappe.get_all(
		_perm_source(doctype),
		filters={"parent": doctype, "permlevel": 0},
		fields=["role", "write", "create"],
	)
	return {r.role for r in rows if (r.write or r.create) and r.role not in ADMIN_ROLES}


def _executable_fields():
	"""doctype -> the Code fields on it, for every non-child doctype in the live DB."""
	found = {}
	for source, parent in (("DocField", "parent"), ("Custom Field", "dt")):
		for row in frappe.get_all(
			source, filters={"fieldtype": "Code"}, fields=[f"{parent} as dt", "fieldname", "options"]
		):
			found.setdefault(row.dt, []).append((row.fieldname, row.options or ""))
	return found


def _roles_writing_at(doctype, permlevel):
	"""Non-admin roles holding WRITE at exactly `permlevel`."""
	rows = frappe.get_all(
		_perm_source(doctype),
		filters={"parent": doctype, "permlevel": permlevel, "write": 1},
		fields=["role"],
	)
	return {r.role for r in rows if r.role not in ADMIN_ROLES}


def _non_admin_field_authors(doctype, fieldname):
	"""Non-admin roles that may actually write THIS field. A permlevel-1 field needs write at BOTH its
	own level and level 0, so raising a field's permlevel is a real lock and the sweep must honour it."""
	permlevel = frappe.get_meta(doctype).get_field(fieldname).permlevel or 0
	authors = _non_admin_authors(doctype)
	return authors if permlevel == 0 else authors & _roles_writing_at(doctype, permlevel)


def _writable_at_permlevel(doctype, fieldname, role):
	"""True when `role` may WRITE `fieldname`, resolving the field's permlevel."""
	return role in _non_admin_field_authors(doctype, fieldname)


class TestExecutableFieldsAreAdminOnly(FrappeTestCase):
	"""Lens 1 — who may write a field that runs."""

	def test_no_unreviewed_role_may_author_an_executable_field(self):
		"""The standing sweep. A new app, a version bump or a new Code field lands here first."""
		violations = []
		for doctype, fields in _executable_fields().items():
			if not frappe.db.exists("DocType", doctype) or frappe.db.get_value("DocType", doctype, "istable"):
				continue
			allowed = REVIEWED_EXCEPTIONS.get(doctype, {})
			for fieldname, language in fields:
				if language.replace(" ", "").lower() not in EXEC_LANGUAGES:
					continue
				for role in sorted(_non_admin_field_authors(doctype, fieldname) - set(allowed)):
					violations.append((doctype, fieldname, role))
		self.assertFalse(
			violations,
			"a non-admin role may author a field that EXECUTES. Lock it, or add it to "
			f"REVIEWED_EXCEPTIONS with a reason: {sorted(violations)}",
		)

	def test_a_custom_html_block_is_authored_by_no_one(self):
		"""Custom HTML Block is killed — no non-admin role may author OR read it.
		No embedded block is expected on any workspace any more."""
		authors = _non_admin_authors("Custom HTML Block")
		self.assertFalse(authors, f"Custom HTML Block may be authored by {sorted(authors)}")
		readers = {
			r.role for r in frappe.get_all(
				_perm_source("Custom HTML Block"),
				filters={"parent": "Custom HTML Block", "permlevel": 0, "read": 1},
				fields=["role"],
			) if r.role not in ADMIN_ROLES
		}
		self.assertFalse(readers,
						 f"Custom HTML Block may be read by {sorted(readers)} — the doctype is dead")

	def test_wiki_settings_javascript_is_not_writable_by_a_wiki_approver(self):
		self.assertFalse(_writable_at_permlevel("Wiki Settings", "javascript", "Wiki Approver"))
		self.assertFalse(_writable_at_permlevel("Wiki Settings", "head_html", "Wiki Approver"))

	def test_lms_batch_script_fields_are_not_writable_by_a_moderator(self):
		for fieldname in ("custom_script", "custom_component"):
			self.assertFalse(_writable_at_permlevel("LMS Batch", fieldname, "Moderator"), fieldname)
			self.assertFalse(_writable_at_permlevel("LMS Batch", fieldname, "Batch Evaluator"), fieldname)

	def test_an_agent_may_read_but_not_author_a_ticket_template(self):
		self.assertNotIn("Agent", _non_admin_authors("HD Ticket Template"))
		self.assertTrue(
			frappe.db.exists(_perm_source("HD Ticket Template"), {"parent": "HD Ticket Template", "role": "Agent", "read": 1})
		)


class TestProfileLinkScheme(FrappeTestCase):
	"""Lens 2 — what may be stored, whoever stores it."""

	# Their own case-mixed probe, plus the tab form a browser strips before reading the scheme.
	UNSAFE = (
		"javascript:alert(1)",
		"jaVaScRiPt:alert(1)",
		"java\tscript:alert(1)",
		"  javascript:alert(1)",
		"data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
		"vbscript:msgbox(1)",
		"http://example.test/x",
		"HTTP://EXAMPLE.TEST/X",
	)
	SAFE = ("", "https://linkedin.com/in/someone", "https://example.test/x")

	def _user(self):
		email = "link.scheme.probe@example.test"
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		doc = frappe.get_doc({
			"doctype": "User", "email": email, "first_name": "link probe",
			"user_type": "System User", "send_welcome_email": 0,
		}).insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("User", email, force=True, ignore_permissions=True))
		return doc

	def test_an_executable_scheme_is_refused_on_every_profile_link(self):
		"""Driven through a real .save(), because a pure-function test proves nothing about WHEN it runs."""
		doc = self._user()
		for fieldname in LINK_FIELDS:
			if not doc.meta.get_field(fieldname):
				continue
			for payload in self.UNSAFE:
				doc.set(fieldname, payload)
				with self.assertRaises(frappe.ValidationError, msg=f"{fieldname} accepted {payload!r}"):
					doc.save(ignore_permissions=True)
				doc.reload()

	def test_an_ordinary_web_address_is_accepted(self):
		doc = self._user()
		for fieldname in LINK_FIELDS:
			if not doc.meta.get_field(fieldname):
				continue
			for payload in self.SAFE:
				doc.set(fieldname, payload)
				doc.save(ignore_permissions=True)
				self.assertEqual(frappe.db.get_value("User", doc.name, fieldname) or "", payload)

	def test_the_scheme_is_read_the_way_a_browser_reads_it(self):
		for payload in self.UNSAFE:
			self.assertFalse(is_safe_scheme(payload), payload)
		for payload in self.SAFE:
			self.assertTrue(is_safe_scheme(payload), payload)
