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
		"""A Custom Field is exported either by its doctype (the `dt in [...]` entry) or by name. A field on a
		SHARED doctype — one we do not own — is covered only if it is named."""
		rows = json.loads((_APP / "fixtures" / "custom_field.json").read_text())
		by_doctype = _dt_in_filter()
		by_name = _named_in_filter("Custom Field")
		orphaned = sorted(
			r["name"] for r in rows if r["dt"] not in by_doctype and r["name"] not in by_name
		)
		self.assertEqual(
			orphaned,
			[],
			"these Custom Fields are in the fixture file but covered by NEITHER the `dt in [...]` entry nor "
			f"the name list — the next export drops them: {orphaned}",
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
