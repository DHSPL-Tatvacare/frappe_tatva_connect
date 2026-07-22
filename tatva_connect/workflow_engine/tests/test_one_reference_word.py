# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""ONE WORD FOR A NAMESPACED REFERENCE, SO THE GRAPH EXPORTS AS ONE DOCUMENT.

The contract flows upstream to downstream: what a node declares it EMITS must be what a downstream node
can READ, in the same shape, under the same word. It was not:

    refs.of_node("send-1", "status")  ->  "send-1.status"      ONE namespaced string
    upstream._shaped(...)             ->  {"key": …}           a node WRITES it  - called `key`
    contract.reads_of(...)            ->  {"name": …}          a node READS it   - called `name`

The same string, two words, depending on which direction you looked. A picker offering `key` and a gate
checking `name` are two brains that agree only by luck.

THE WORD IS `ref`, and it is not a new one: `refs.py` owns the concept and already uses it — `parse(ref)`,
`before(ref)`, `is_reference(value)`, and a docstring reading "A reference is <source>.<field>". `key` and
`name` are later inventions by consumers of a brain that had already named the thing.

THE SEAM, and it is the whole scope question. `key` means TWO things here:

    describe.py:158   "key": df.fieldname                    a BARE field name
    refs.py:155       "key": of_record(doctype, f["key"])    a NAMESPACED ref

`refs.readable_for` is where one becomes the other: a bare key goes in, a `ref` comes out. Everything
downstream of that line is in scope. Describe's bare key is NOT — twelve modules outside this engine
consume it, and it is a different concept that deserves its own word on its own day.
"""
import json
import pathlib
import re
import unittest

from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import contract, graph, refs, upstream

_SUBJECT = "CRM Lead"


def _node(node_id, node_type, config=None, edges=None):
	return {
		"node_id": node_id, "node_type": node_type,
		"config_json": json.dumps(config or {}),
		"edges": [{"from_output": o, "to_node": t} for o, t in (edges or {}).items()],
	}


def _graph():
	"""Trigger → Call API (emits) → Branch reading what it emitted → End. One graph that both WRITES and
	READS a namespaced reference, which is the only way the two words can be compared."""
	return [
		_node("trigger-1", "Trigger", {"subject_doctype": _SUBJECT, "event": "Created"}, {"next": "api-1"}),
		_node("api-1", "Call API", {"webhook_endpoint": "x"}, {"succeeded": "b1", "failed": "b1"}),
		_node("b1", "Branch",
		      {"condition": {"type": "rule", "field": "api-1.status", "operator": "is", "value": 200}},
		      {"true": "end-1", "false": "end-1"}),
		_node("end-1", "Terminal"),
	]


class TestTheWrittenAndTheReadCarryOneWord(FrappeTestCase):
	"""THE red: `upstream` said `key` and `contract` said `name` for the same string."""

	def test_what_a_node_offers_downstream_is_keyed_ref(self):
		offered = upstream.available_at(_graph(), "b1")
		self.assertTrue(offered, "the fixture must offer something or this proves nothing")
		for value in offered:
			self.assertIn("ref", value, f"an offered value is not keyed `ref`: {sorted(value)}")
			self.assertNotIn("key", value, "the old spelling survives in what a node offers")

	def test_what_a_node_reads_is_keyed_ref(self):
		found = contract.reads_of("Branch", {"condition": {
			"type": "rule", "field": "api-1.status", "operator": "is", "value": 200,
		}})
		self.assertTrue(found)
		for read in found:
			self.assertIn("ref", read, f"a read reference is not keyed `ref`: {sorted(read)}")
			self.assertNotIn("name", read, "the old spelling survives in what a node reads")

	def test_the_same_string_carries_the_same_word_in_both_directions(self):
		"""THE HEADLINE. Written and read are the same concept, so they must be the same word."""
		written = {v["ref"] for v in upstream.available_at(_graph(), "b1")}
		read = {r["ref"] for r in contract.reads_of("Branch", {"condition": {
			"type": "rule", "field": "api-1.status", "operator": "is", "value": 200,
		}})}
		self.assertTrue(read & written, "a reference a node reads is not among what upstream offers")


class TestTheSeamIsExplicit(FrappeTestCase):
	"""Bare goes in, `ref` comes out. Proven rather than remembered, because this line is the boundary
	the whole scope decision rests on."""

	def test_readable_for_takes_a_bare_key_and_emits_a_ref(self):
		found = refs.readable_for(_SUBJECT)
		self.assertTrue(found)
		for row in found:
			self.assertIn("ref", row)
			self.assertIn(refs.SEP, row["ref"], "what leaves the seam must be namespaced")

	def test_describe_still_speaks_its_own_bare_word_on_the_other_side(self):
		"""OUT OF SCOPE and asserted so, because renaming it would collide with live work in twelve
		modules outside this engine. The seam is what makes leaving it correct rather than sloppy."""
		from tatva_connect.automation import describe

		catalog = describe.fields_for_doctype(_SUBJECT)
		self.assertTrue(catalog)
		self.assertIn("key", catalog[0], "describe's own bare vocabulary must be left alone")
		self.assertNotIn(refs.SEP, catalog[0]["key"], "describe emits a BARE name, not a ref")


class TestTheGraphExportsAsOneDocument(FrappeTestCase):
	"""THE USER'S ACTUAL REQUIREMENT: pin the JSON and it reads as one document. Serialises everything a
	graph produces — what each node offers, what each node reads, and the problems it raises — and asserts
	one vocabulary across all of it."""

	def _export(self):
		nodes = _graph()
		return {
			"nodes": [{"node_id": n["node_id"], "node_type": n["node_type"]} for n in nodes],
			"offers": {n["node_id"]: upstream.available_at(nodes, n["node_id"]) for n in nodes},
			"reads": {
				n["node_id"]: contract.reads_of(n["node_type"], json.loads(n["config_json"]))
				for n in nodes
			},
			"problems": graph.problems(nodes, entry_node="trigger-1"),
		}

	def test_no_namespaced_reference_is_called_anything_but_ref(self):
		exported = json.loads(json.dumps(self._export()))
		offenders = []
		for node_id, values in exported["offers"].items():
			offenders += [f"offers[{node_id}] {sorted(v)}" for v in values if "ref" not in v]
		for node_id, values in exported["reads"].items():
			offenders += [f"reads[{node_id}] {sorted(v)}" for v in values if "ref" not in v]
		self.assertEqual(offenders, [], f"the export speaks more than one word: {offenders}")

	def test_the_export_really_carries_both_directions(self):
		"""A document with nothing read in it would pass the test above by being empty."""
		exported = self._export()
		self.assertTrue(any(exported["offers"].values()), "nothing is offered")
		self.assertTrue(any(exported["reads"].values()), "nothing is read")


class TestEmitsRoundTrip(FrappeTestCase):
	"""What a node type DECLARES it emits is exactly what a downstream node is offered — no more, no less,
	same word. What is added to `emits` MUST be emitted."""

	def test_every_declared_emit_is_offered_downstream_under_its_node_id(self):
		from tatva_connect.automation import actions

		nodes = _graph()
		offered = {v["ref"] for v in upstream.available_at(nodes, "b1")}
		declared = actions.emits_of("Call API", {"webhook_endpoint": "x"})
		self.assertTrue(declared, "the fixture node must declare emits or this proves nothing")
		for value in declared:
			with self.subTest(emit=value["name"]):
				self.assertIn(refs.of_node("api-1", value["name"]), offered)

	def test_nothing_is_offered_that_no_node_declared_and_the_subject_does_not_have(self):
		"""The other direction: `no more`. Every offered ref is either a declared emit or a subject field."""
		from tatva_connect.automation import actions

		nodes = _graph()
		declared = {refs.of_node("api-1", v["name"]) for v in actions.emits_of("Call API", {"webhook_endpoint": "x"})}
		subject = {f["ref"] for f in refs.readable_for(_SUBJECT)}
		stray = [v["ref"] for v in upstream.available_at(nodes, "b1") if v["ref"] not in declared | subject]
		self.assertEqual(stray, [], f"offered but declared by nothing: {stray}")


class TestTheOldSpellingIsGone(unittest.TestCase):
	"""B12 — forbids the SHAPE, and the SCAN SURFACE IS ASSERTED FIRST. One lock on this surface already
	read as coverage it did not have; this one names the files it checks and proves they are the right
	ones before trusting the pattern."""

	_ENGINE = pathlib.Path(upstream.__file__).parent
	_SCOPED = ("upstream.py", "contract.py", "refs.py", "graph.py", "context.py")
	# A namespaced ref being built or read under the old words. `f["key"]` inside `readable_for` is the
	# SEAM - describe's bare key going in - so the pattern targets the ref-shaped dict keys only.
	_OLD = re.compile(r"""["']key["']\s*:\s*of_|\bvalue\[["']key["']\]|\bref\[["']name["']\]|["']name["']\s*:\s*name\b""")

	def test_the_scan_surface_is_the_modules_that_carry_the_reference(self):
		present = {p.name for p in self._ENGINE.glob("*.py")}
		for name in self._SCOPED:
			self.assertIn(name, present, f"the lock points at {name}, which is not in the engine")
		self.assertIn("of_node", (self._ENGINE / "refs.py").read_text(), "refs.py is not the ref brain here")

	def test_no_namespaced_reference_is_spelled_key_or_name(self):
		offenders = []
		for name in self._SCOPED:
			for n, line in enumerate((self._ENGINE / name).read_text().splitlines(), 1):
				if self._OLD.search(line) and not line.strip().startswith("#"):
					offenders.append(f"{name}:{n}")
		self.assertEqual(offenders, [], f"a namespaced ref is still spelled key/name at: {offenders}")
