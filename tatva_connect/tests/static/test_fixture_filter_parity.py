# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every fixture row must be covered by the hooks filter that exports it.

`bench export-fixtures` REWRITES each fixture file from the filter in hooks.py. A row sitting in the
file whose name the filter does not list is therefore dropped the next time anyone exports — silently,
and with a clean diff that looks like tidying. That is not hypothetical: it is how the permlevel-1 lock
on Product Line and Group came to be lost, and a rep could move a lead into another business until it
was found.

The dangerous direction is IN FILE -> NOT NAMED, and that is what fails here. The reverse (the filter
naming a row no file carries) is harmless: `schema_setup` creates the naming setters on every migrate,
and a filter may name a row that does not exist yet.

No bench, no frappe — this reads the two files and compares them.
"""
import ast
import json
import re
import unittest
from pathlib import Path

_APP = Path(__file__).resolve().parents[2]
_HOOKS = _APP / "hooks.py"

# Doctypes another app declares that we nonetheless export wholesale, each because we are in practice its
# only author. A judgement per entry, so it is written down rather than inferred.
_KNOWN_BORROWED = [
	"CRM Call Log",       # crm's; our telephony spine is the only thing that extends it
	"CRM Dashboard",      # crm's; the role-layout fields are entirely ours
	"CRM Deal",           # crm's; the grain and profile fields are entirely ours
	"CRM Lead",           # crm's; the whole patient record is our extension of it
	"CRM Task",           # crm's; the activity engine is our extension of it
	"CRM Telephony Agent",  # crm's; extended only by our telephony spine
	"File",               # frappe's; only our storage layer adds to it
	"WhatsApp Account",   # frappe_whatsapp's; only our provider spine adds to it
]


def _our_doctypes() -> set:
	"""Every doctype THIS app declares, read from the module folders on disk."""
	return {
		path.name
		for module in _APP.iterdir()
		if (module / "doctype").is_dir()
		for path in (module / "doctype").iterdir()
		if path.is_dir()
	}


def _scrub(doctype: str) -> str:
	"""Frappe's own folder-name rule for a doctype, without importing frappe."""
	return doctype.lower().replace(" ", "_").replace("-", "_")


def _named_in_filter(marker: str) -> set:
	"""The names listed in a `{"dt": <doctype>, "filters": [["name", "in", [...]]]}` fixture entry."""
	hooks = _HOOKS.read_text()
	chunks = hooks.split(f'{{"dt": "{marker}", "filters": [["name", "in", [')
	names = set()
	for chunk in chunks[1:]:
		block = chunk.split("]]]}")[0]
		names |= set(re.findall(r'"([^"]+)"', block))
	return names


def _dt_in_filter() -> set:
	"""The doctypes a `[["dt", "in", [...]]]` Custom Field entry exports wholesale (every field on them)."""
	hooks = _HOOKS.read_text()
	block = hooks.split('["dt", "in", [')[1].split("]]")[0]
	return set(re.findall(r'"([^"]+)"', block))


class TestFixtureFilterParity(unittest.TestCase):
	def test_every_property_setter_in_the_file_is_named_in_the_filter(self):
		rows = json.loads((_APP / "fixtures" / "property_setter.json").read_text())
		named = _named_in_filter("Property Setter")
		orphaned = sorted({r["name"] for r in rows} - named)
		self.assertEqual(
			orphaned,
			[],
			"these Property Setters are in the fixture file but NOT named in the hooks filter — the next "
			f"`bench export-fixtures` drops them and a fresh install never gets them: {orphaned}",
		)

	def test_every_custom_field_in_the_file_is_covered_by_the_filter(self):
		"""A Custom Field is exported either by its doctype (the `dt in [...]` entry) or by name.

		The failure names the bucket, not just the gap. Which one a field belongs in is decided by WHO OWNS
		the doctype, and that is a fact on disk, so the check reads it rather than asking anyone to remember:
		a doctype this app declares can be exported wholesale (no other app writes fields to it), and one
		somebody else declares must be listed field by field or the export sweeps up the owner's fields too.
		"""
		rows = json.loads((_APP / "fixtures" / "custom_field.json").read_text())
		by_doctype = _dt_in_filter()
		by_name = _named_in_filter("Custom Field")
		ours = _our_doctypes()

		wholesale, individually = set(), []
		for row in rows:
			if row["dt"] in by_doctype or row["name"] in by_name:
				continue
			(wholesale.add(row["dt"]) if _scrub(row["dt"]) in ours else individually.append(row["name"]))

		problem = ""
		if wholesale:
			problem += (
				"\nAdd these doctypes to the Custom Field `dt in [...]` entry — this app declares them, so "
				f"every custom field on them is ours: {sorted(wholesale)}"
			)
		if individually:
			problem += (
				"\nAdd these fields BY NAME to the Custom Field name list — another app declares the doctype, "
				f"so a wholesale export would sweep up its own fields: {sorted(individually)}"
			)
		self.assertEqual(
			(wholesale, individually),
			(set(), []),
			"these Custom Fields are in the fixture file but covered by neither entry, so the next "
			f"`bench export-fixtures` drops them and a fresh install never gets them:{problem}",
		)

	def test_the_wholesale_list_only_names_doctypes_this_app_owns(self):
		"""The other direction: exporting a doctype wholesale is safe ONLY while we are its only author.
		Naming somebody else's doctype there makes the next export claim their fields as ours."""
		ours = _our_doctypes()
		borrowed = sorted(dt for dt in _dt_in_filter() if _scrub(dt) not in ours)
		self.assertEqual(
			borrowed,
			_KNOWN_BORROWED,
			"the Custom Field `dt in [...]` entry exports every field on a doctype another app declares. "
			"That is only safe where we are in practice its only author, which is a judgement — so each one "
			f"is listed in _KNOWN_BORROWED with its reason. Unlisted: {sorted(set(borrowed) - set(_KNOWN_BORROWED))}",
		)

	def test_the_hooks_filters_are_still_the_shape_this_test_reads(self):
		"""If the fixture entries are ever restructured, this test must fail LOUDLY rather than pass by
		reading nothing — a parity check that silently matches an empty set is worse than none."""
		self.assertTrue(_named_in_filter("Property Setter"), "no Property Setter name filter found in hooks.py")
		self.assertTrue(_named_in_filter("Custom Field"), "no Custom Field name filter found in hooks.py")
		self.assertTrue(_dt_in_filter(), "no Custom Field `dt in [...]` filter found in hooks.py")
		ast.parse(_HOOKS.read_text())  # hooks.py must at least be valid python


if __name__ == "__main__":
	unittest.main()
