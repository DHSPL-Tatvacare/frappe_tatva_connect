# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A search index that cannot be READ is repaired, and nothing on a rep's path pays for the check.

THE DISEASE. Frappe's 3-hourly `build_index_if_not_exists` recovers two states: a build interrupted midway
(a temp file survives) and no index at all. A file that exists but is DAMAGED passes both — it is on disk and
it still carries `search_fts`, so `index_exists()` says yes and the schema fingerprint still matches. Nothing
else ever looks. Meanwhile every read fails (`sqlite_search.py:272` logs and returns empty) and every write
fails (`index_doc` / `remove_doc` log and move on), so nobody is blocked and nobody is told.

Measured on this bench: one index was damaged on 2026-07-26 and was still returning nothing on 2026-07-30,
41 Error Log entries later. Search had been dead for four days.

THE CURE, and why it is shaped this way. `sweep_index_health` drops the unreadable file and hands the rebuild
back to `build_index_in_background` — frappe's own entry point, which enqueues under a `job_id` with
`deduplicate=True`. One builder, one job id, so frappe's next pass collapses into the same job instead of
racing it. A second builder here could write the very corruption it repairs, because both would use the same
temp path.

THE CHECK IS NOT ON THE HOT PATH, and that is the whole reason it is a separate method. `update_doc_index`
and `delete_doc_index` are hooked into `doc_events["*"]`, so `index_exists()` runs on EVERY save of EVERY
doctype site-wide. Putting an integrity pragma there would scan a multi-megabyte file on every save a rep
makes. `test_the_health_check_is_not_on_any_save_path` is what keeps it out.

Run:
    bench --site <site> run-tests --app tatva_connect \
        --module tatva_connect.tests.search.test_index_health_sweep
"""
import inspect
import os

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.search import index as search_index
from tatva_connect.search.index import CRMLeadSearch


def _scribble(path):
	"""Damage a page in the middle of the b-tree — what an interrupted write leaves behind."""
	size = os.path.getsize(path)
	with open(path, "r+b") as f:
		f.seek(size // 3)
		f.write(b"\x00" * 4096)


class TestIndexHealthSweep(FrappeTestCase):
	"""The damaged state is detected, repaired, and paid for only by the scheduler."""

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.engine = CRMLeadSearch()
		self.db_path = self.engine.db_path
		self.backup = f"{self.db_path}.zz-health-test-backup"

		# The rebuild is RECORDED, not run: an enqueued build's timing is not ours, and letting it loose made `test_row_shape` fail in a full run and pass alone. Recording is also the stronger claim — that this module starts no builder of its own.
		import frappe.search.sqlite_search as native

		self.rebuilds = []
		original = native.build_index_in_background
		native.build_index_in_background = lambda *a, **kw: self.rebuilds.append(True)
		self.addCleanup(lambda: setattr(native, "build_index_in_background", original))

		# Put back byte for byte: the index is rebuildable, but a real rebuild is slow and this is a shared file.
		if os.path.exists(self.db_path):
			import shutil

			shutil.copy2(self.db_path, self.backup)
			self.addCleanup(self._restore)

	def _restore(self):
		import shutil

		for leftover in (self.engine._get_db_path(is_temp=True), f"{self.db_path}-wal", f"{self.db_path}-shm"):
			if os.path.exists(leftover):
				os.unlink(leftover)
		if os.path.exists(self.backup):
			shutil.move(self.backup, self.db_path)

	def test_the_premise(self):
		"""There must be a readable index to damage, or nothing below proves anything."""
		self.assertTrue(self.engine.index_exists(), "no index on this site — build one before running this")
		self.assertTrue(self.engine.index_is_readable(), "the index is ALREADY damaged; repair it first")

	def test_a_damaged_index_is_seen_as_unreadable(self):
		"""The detector. RED before `index_is_readable` existed: nothing asked this question at all."""
		_scribble(self.db_path)

		self.assertFalse(CRMLeadSearch().index_is_readable(),
						 "a damaged index reported itself readable — the check does not detect the failure "
						 "mode that silently killed search for four days")

	def test_a_damaged_index_still_passes_every_check_frappe_makes(self):
		"""WHY the gap exists, asserted rather than described. If frappe ever starts catching this, this test
		goes red and the sweep can be deleted."""
		_scribble(self.db_path)
		engine = CRMLeadSearch()

		self.assertTrue(engine.index_exists(),
						"frappe now detects a damaged index; the sweep is redundant and should be removed")
		self.assertFalse(os.path.exists(engine._get_db_path(is_temp=True)),
						 "a temp file exists, so frappe's continuation branch would have covered this")

	def test_the_sweep_drops_a_damaged_index_and_hands_the_rebuild_back(self):
		"""The cure, both halves. The file must be GONE — that is the one state frappe's own job rebuilds
		from — and the rebuild must be frappe's, enqueued once, never a builder of our own."""
		_scribble(self.db_path)

		search_index.sweep_index_health()

		self.assertFalse(os.path.exists(self.db_path),
						 "the sweep left a damaged index in place, so search stays dead until someone notices")
		self.assertEqual(len(self.rebuilds), 1,
						 "the rebuild was not handed to frappe's own entry point exactly once — two builders "
						 "would share one temp path and could write the corruption this repairs")

	def test_the_sweep_leaves_a_healthy_index_alone(self):
		"""The regression that would matter most: dropping a good index on every pass would rebuild the
		whole thing hourly and blank search while it ran."""
		search_index.sweep_index_health()

		self.assertTrue(os.path.exists(self.db_path), "the sweep dropped a HEALTHY index")
		self.assertTrue(CRMLeadSearch().index_is_readable(), "the sweep damaged a healthy index")
		self.assertEqual(self.rebuilds, [], "the sweep rebuilt a healthy index")

	def test_the_sweep_stands_off_while_a_build_owns_the_index(self):
		"""A temp file means a builder is mid-swap. Dropping the live file under it would strand the swap,
		and frappe's own first branch already continues such a build."""
		_scribble(self.db_path)
		temp = self.engine._get_db_path(is_temp=True)
		open(temp, "wb").close()
		self.addCleanup(lambda: os.path.exists(temp) and os.unlink(temp))

		search_index.sweep_index_health()

		self.assertTrue(os.path.exists(self.db_path),
						"the sweep dropped the live index while a build was in flight")
		self.assertEqual(self.rebuilds, [], "the sweep enqueued a second build over one already running")

	def test_the_sweep_is_dormant_with_the_feature(self):
		"""Every switch in this app ships off, and a disabled feature has no index to keep healthy."""
		self.assertIn("is_search_enabled", inspect.getsource(search_index.sweep_index_health),
					  "the sweep runs regardless of the feature toggle")

	def test_the_health_check_is_not_on_any_save_path(self):
		"""THE guard against the naive version of this fix.

		`update_doc_index` / `delete_doc_index` are in frappe's `doc_events["*"]`, so `index_exists()` runs on
		every save of every doctype site-wide. Had the pragma gone in there, every rep's save would scan the
		whole index file. It must stay out of `index_exists` and out of `is_search_enabled`."""
		for method in (CRMLeadSearch.index_exists, CRMLeadSearch.is_search_enabled):
			source = inspect.getsource(method) if method.__qualname__.startswith("CRMLeadSearch") else ""
			self.assertNotIn("quick_check", source,
							 f"{method.__qualname__} runs a health pragma, and it is called on every save")
		self.assertNotIn("index_is_readable", inspect.getsource(search_index.reindex_lead),
						 "the per-lead reindex now pays for a health check")

	def test_only_the_sweep_asks_the_health_question(self):
		"""One caller, so the cost stays where it was measured. A second caller is a second decision about
		when this is affordable, and that is how a 5 ms check ends up on a save."""
		callers = [
			name for name, obj in vars(search_index).items()
			if callable(obj) and getattr(obj, "__module__", "") == search_index.__name__
			and "index_is_readable" in (inspect.getsource(obj) if not isinstance(obj, type) else "")
		]

		self.assertEqual(callers, ["sweep_index_health"],
						 f"index_is_readable is asked by more than the sweep: {callers}")
