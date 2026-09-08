# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A THREAD UPDATE REACHES THE PEOPLE WATCHING THAT LEAD, NOT EVERY LOGGED-IN REP.

`frappe.publish_realtime` picks the room itself, and its LAST resort is the whole site:

    else:
        # This will be broadcasted to all Desk users
        room = get_site_room()                      # frappe/realtime.py

So an emit that names neither `doctype`/`docname` nor `user` is a site-wide broadcast. Two of ours did:
the historical re-publish after an ingest, and the one the manual refresh sends when it finishes. Each
carried a lead's id to every open Desk session and made every one of them refetch a thread nobody was
looking at — on a team of reps, one patient's reply woke all of them.

WHY THE ROOM IS THE RIGHT FIX AND A FILTER IS NOT: socketio admits a client to a doc room only after
`doc_subscribe` has checked it can READ that record, so the scope IS the permission rather than a
condition we remembered to write. `_publish_refresh` already did this and says so; the other two were
simply missed.

Asserted on the CALL, because the room is chosen inside frappe from these arguments — there is nothing
of ours in between to inspect.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.whatsapp.test_realtime_scope
"""
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.whatsapp import ingest

_LEAD = "Realtime-scope-probe-lead"


class TestRealtimeIsScopedToTheRecord(FrappeTestCase):
	def _emits(self, call):
		"""Every publish_realtime the call made, as (event, kwargs)."""
		with mock.patch.object(frappe, "publish_realtime") as pub:
			call()
		return [(c.args[0] if c.args else c.kwargs.get("event"), c.kwargs) for c in pub.call_args_list]

	def test_the_ingest_republish_names_the_record_it_is_about(self):
		"""THE red. Unroomed, this told every Desk user on the site about one patient's message."""
		emits = self._emits(lambda: ingest._republish([_LEAD]))
		self.assertEqual(len(emits), 1)
		event, kwargs = emits[0]
		self.assertEqual(event, "whatsapp_message")
		self.assertEqual(kwargs.get("doctype"), "CRM Lead")
		self.assertEqual(kwargs.get("docname"), _LEAD)

	def test_one_emit_per_lead_each_naming_its_own(self):
		"""A shared number mirrors onto more than one lead; each watcher hears only about theirs."""
		emits = self._emits(lambda: ingest._republish([_LEAD, "second-lead"]))
		self.assertEqual([k.get("docname") for _e, k in emits], [_LEAD, "second-lead"])

	def test_no_whatsapp_emit_falls_through_to_the_site_room(self):
		"""The lock that catches the NEXT one: frappe picks the site room whenever an emit names no
		record and no user, so every whatsapp emit in these two modules must name one. Read off the
		source rather than a list of call sites, so a new emit is covered the day it is written."""
		import ast
		import pathlib

		unroomed = []
		for path in (
			pathlib.Path(ingest.__file__),
			pathlib.Path(ingest.__file__).parent.parent / "api" / "whatsapp.py",
		):
			tree = ast.parse(path.read_text())
			for node in ast.walk(tree):
				if not isinstance(node, ast.Call):
					continue
				name = getattr(node.func, "attr", None)
				if name != "publish_realtime":
					continue
				named = {kw.arg for kw in node.keywords}
				if not ({"doctype", "docname"} <= named or "user" in named):
					unroomed.append(f"{path.name}:{node.lineno}")
		self.assertEqual(unroomed, [], "these emits broadcast to every Desk user on the site")
