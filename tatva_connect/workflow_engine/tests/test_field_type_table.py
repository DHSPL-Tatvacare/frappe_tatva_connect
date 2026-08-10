# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""ONE TABLE OF FIELD TYPES. Add a type = add a row, and nothing else changes anywhere.

A node setting declares three facts: what KIND it is, whether it READS values produced earlier, and
whether it WRITES values for later. Six switches consumed those facts and none shared a table, so adding
`Button List` in W1.3 meant editing three of them by hand and nothing would have gone red on a miss.

WHY THREE TABLES AND NOT ONE ROW. `type` determines the CONTROL and the CHECK. It does NOT determine
`reads`/`writes`, and that was measured before this was built: one `Code` control carries three different
semantics — `Call API.request_body` reads `ctx_json`, `Set Variables.assign` reads `expression` and
writes `expression_dict`, `Wait.accepts` writes `payload_map`. Putting `reads` in the type row would have
forced all three to share one kind, and the publish gate would then extract references the wrong way for
two of them — silently, which is the failure mode this whole effort exists to remove.

So a type row carries `reads` only when the type can read exactly ONE way (`Variable`, `Predicate`,
`Value Map`). Everything else declares its kind on the FIELD, and the kind resolves through its own table.

THE ACCEPTANCE TEST FOR THE DESIGN IS IN THIS FILE: a declaration using a type no consumer handles goes
red, a consumer handling a type nothing declares goes red, and a field declaring a kind no table knows
goes red.
"""
import json
import pathlib
import re
import unittest

from tatva_connect.workflow_engine import contract, registry, upstream


def _declared_fields():
	for node_type, declared in registry.NODE_TYPES.items():
		for field in declared.get("config") or []:
			yield node_type, field


class TestTheTableIsTheVocabulary(unittest.TestCase):
	def test_every_declared_type_has_a_row(self):
		"""THE red. Sixteen types were declared and no list of them existed."""
		missing = sorted({f["type"] for _, f in _declared_fields()} - set(registry.FIELD_TYPES))
		self.assertEqual(missing, [], f"declared but in no row: {missing}")

	def test_every_row_is_actually_declared_by_something(self):
		"""The other direction. A row nothing uses is a control nobody can reach and a check that never
		runs — it reads as covered while covering nothing."""
		used = {f["type"] for _, f in _declared_fields()}
		orphan = sorted(set(registry.FIELD_TYPES) - used)
		self.assertEqual(orphan, [], f"rows no declaration uses: {orphan}")

	def test_every_row_carries_the_four_facts(self):
		for name, row in registry.FIELD_TYPES.items():
			with self.subTest(type=name):
				self.assertIn("control", row)
				self.assertIn("check", row)
				self.assertIn("primitive", row)
				self.assertIn("reads", row, "None is a valid answer, absence is not")
				# The fifth fact: how the value READS on a canvas card. `None` used to mean "the frontend
				# decides", and the frontend decided `String(value)` — so a Link printed its composite key
				# and a delay printed its JSON. A row must now name one reading, and a type added later
				# cannot ship until it does.
				self.assertTrue(
					set(row.get("summary") or {}) & {"phrase", "count", "as"},
					f"{name} declares no card rendering — name one of phrase / count / as",
				)

	def test_a_check_a_row_names_really_exists(self):
		"""A row may only name a check the module actually has, or validation silently does nothing."""
		for name, row in registry.FIELD_TYPES.items():
			with self.subTest(type=name):
				if row["check"] is None:
					continue
				self.assertTrue(callable(row["check"]), f"{name} names a check that is not callable")


class TestReadsAndWritesResolveThroughTheirOwnTables(unittest.TestCase):
	"""The correction that produced this design, locked so nobody folds it back into one row."""

	def test_one_control_really_does_carry_three_semantics(self):
		"""The measurement the design rests on. If this ever collapses to one, the type row could carry
		`reads` after all — and until then it must not."""
		combos = {
			(f.get("reads"), f.get("writes"))
			for _, f in _declared_fields() if f["type"] == "Code"
		}
		self.assertGreater(len(combos), 1, "Code carries more than one semantic; a type row cannot hold it")

	def test_every_declared_read_kind_is_in_the_table(self):
		declared = {f.get("reads") for _, f in _declared_fields() if f.get("reads")}
		missing = sorted(declared - set(registry.READ_KINDS))
		self.assertEqual(missing, [], f"fields declare read kinds nothing resolves: {missing}")

	def test_every_declared_write_kind_is_in_the_table(self):
		declared = {f.get("writes") for _, f in _declared_fields() if f.get("writes")}
		missing = sorted(declared - set(registry.WRITE_KINDS))
		self.assertEqual(missing, [], f"fields declare write kinds nothing resolves: {missing}")

	def test_a_type_that_implies_a_read_kind_names_a_real_one(self):
		for name, row in registry.FIELD_TYPES.items():
			with self.subTest(type=name):
				if row["reads"] is None:
					continue
				self.assertIn(row["reads"], registry.READ_KINDS)

	def test_a_field_may_not_declare_a_read_kind_its_type_already_implies(self):
		"""Two answers to one question. If the type implies a kind, the field restating it is a place for
		the two to disagree."""
		clashes = [
			f"{nt}.{f['name']}"
			for nt, f in _declared_fields()
			if f.get("reads") and registry.FIELD_TYPES[f["type"]]["reads"]
		]
		self.assertEqual(clashes, [], f"these restate what their type already says: {clashes}")


class TestTheConsumersReadTheTable(unittest.TestCase):
	"""B12 — matched against the declared surface and the CALL, not against a remembered list of files."""

	_ENGINE = pathlib.Path(registry.__file__).parent
	# The DISPATCH consumers — the ones that chose a control or a check by type name. `graph.py` is out of
	# scope on purpose: its two comparisons express whole-graph write semantics (may this Target be
	# reached, is this Field settable), not control/check dispatch, and folding them in would need the
	# table to carry facts about writing that no other consumer reads.
	_DISPATCH = ("registry.py", "contract.py", "upstream.py")

	def test_no_dispatch_consumer_keeps_its_own_switch_on_field_type(self):
		"""The if-chains this chunk deletes. A `field["type"] == "..."` here is a second vocabulary, and
		the next type added will miss it exactly as `Button List` did."""
		offenders = []
		for path in self._ENGINE.rglob("*.py"):
			if "/tests/" in str(path) or path.name not in self._DISPATCH:
				continue
			for n, line in enumerate(path.read_text().splitlines(), 1):
				if re.search(r'\bfield\[.type.\]\s*==\s*[\'"]', line) or re.search(r'\bf\[.type.\]\s*==\s*[\'"]', line):
					offenders.append(f"{path.name}:{n}")
		self.assertEqual(offenders, [], f"switches on field type still exist: {offenders}")

	def test_reads_resolve_through_the_table_for_every_declared_field(self):
		"""Drives the real extractor for every declared reading field, so a kind whose resolver is wired to
		the wrong extractor cannot pass by being unreached."""
		for node_type, field in _declared_fields():
			kind = registry.FIELD_TYPES[field["type"]]["reads"] or field.get("reads")
			if not kind:
				continue
			with self.subTest(node=node_type, field=field["name"]):
				self.assertIn(kind, registry.READ_KINDS)
				self.assertIsInstance(contract._references(field, None), set)

	def test_writes_resolve_through_the_table(self):
		for node_type, field in _declared_fields():
			if not field.get("writes"):
				continue
			with self.subTest(node=node_type, field=field["name"]):
				self.assertIn(field["writes"], upstream.registry.WRITE_KINDS)


class TestTheSmallDuplicationsAreGone(unittest.TestCase):
	"""The refactor pass that belongs to this seam — it will not come round again."""

	_APP = pathlib.Path(registry.__file__).parents[1]

	def test_config_json_has_one_reader(self):
		"""It was written out five times. A sixth copy is where the next parse bug lives."""
		hits = []
		for path in self._APP.rglob("*.py"):
			if "/tests/" in str(path) or path.name == "registry.py":
				continue
			for n, line in enumerate(path.read_text().splitlines(), 1):
				if "config_json" in line and "parse_json" in line:
					hits.append(f"{path.name}:{n}")
		self.assertEqual(hits, [], f"config_json is still parsed inline at: {hits}")

	def test_ctx_prefix_has_one_spelling(self):
		"""`contract._CTX_PREFIX` was declared and nothing used it, while `actions` spelt it inline."""
		hits = []
		for path in self._APP.rglob("*.py"):
			if "/tests/" in str(path) or path.name in ("contract.py", "refs.py"):
				continue
			for n, line in enumerate(path.read_text().splitlines(), 1):
				if '"$ctx."' in line or "'$ctx.'" in line:
					hits.append(f"{path.name}:{n}")
		self.assertEqual(hits, [], f"$ctx. is spelt inline at: {hits}")

	def test_an_unparseable_expression_is_refused_at_publish(self):
		"""`expr.assert_parses` was DEAD — its only caller was a test, which is why publish never syntax
		checked an author's expression. It is now the `reads=expression` row's check."""
		row = registry.READ_KINDS["expression"]
		self.assertTrue(callable(row["check"]), "the expression kind must carry a check")
		self.assertTrue(row["check"]("for x in y: pass"), "a statement is not an eval expression")
		self.assertFalse(row["check"]("ctx['x'] + 1"), "a real expression must pass")


class TestTheFrontendRendersFromTheSameTable(unittest.TestCase):
	"""The Vue chain was the sixth consumer and the one nothing could lock from Python. The table ships on
	the wire, so the inspector looks a control up instead of carrying its own `v-if` ladder."""

	_INSPECTOR = pathlib.Path(
		"/home/frappe/frappe-bench/apps/crm/frontend/src/tatva/workflows/NodeInspector.vue"
	)

	def test_every_field_reaches_the_wire_carrying_its_control(self):
		"""C17.2 — a backend table growing a column the canvas reads is only HALF a change until
		`node_types()` ships it. `FIELD_TYPES` gained `control`, the wire did not carry it, and every
		domain control in the inspector silently became an empty grey box: no JS error, clean build,
		green suite. This walks the real payload rather than the table."""
		for node in registry.node_types():
			for field in node.get("config") or []:
				with self.subTest(node=node["type"], field=field["name"]):
					self.assertIn("control", field, "the inspector reads f.control and would get undefined")
					self.assertIn("primitive", field)
					self.assertEqual(
						field["control"], registry.FIELD_TYPES[field["type"]]["control"],
						"the wire disagrees with the table it is built from",
					)

	def test_the_table_reaches_the_wire(self):
		payload = registry.field_types()
		self.assertTrue(payload, "the inspector cannot look up what is never shipped")
		for name, row in payload.items():
			with self.subTest(type=name):
				self.assertIn("control", row)
				self.assertIn("primitive", row)

	def test_the_inspector_no_longer_switches_on_each_type_by_name(self):
		if not self._INSPECTOR.exists():
			self.skipTest("frontend not mounted in this container")
		source = self._INSPECTOR.read_text()
		named = re.findall(r"f\.type === '([^']+)'", source)
		self.assertEqual(named, [], f"the inspector still names types itself: {sorted(set(named))}")
