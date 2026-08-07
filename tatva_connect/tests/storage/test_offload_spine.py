# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The offload GATE: which files `after_insert` decides to offload — not whether offloading works.

WHAT THIS PROVES AND WHAT IT DOES NOT. Every case here drives `after_insert` with a `frappe._dict`
and asserts that `offload` was CALLED. No File row is written, no bytes move, Azure is never reached.
So this is a decision table for the gate and nothing more: if `offload()` itself broke, every test
here would stay green. The OUTCOME — bytes really in the container, the row really repointed, a
consumer really able to read them — is proven in `test_file_layer_registry` and `test_call_media`
against real Azure, and that is where a regression in offloading would surface.

The gate itself: `after_insert` must decide to offload ANY file with local bytes, regardless
of its `folder` or whether it is attached. The removed `_is_compose_draft` skip (which
deferred anything in "Home" or "Home/Email Drafts") was a per-uploader lottery on
frappe's default "Home" folder — it silently stranded notes / form-field / helpdesk
attachments locally. Transient/discarded files are reclaimed by on_trash (ref-counted),
never by skipping the upload.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.storage.test_offload_spine
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.storage import file_events


class TestOffloadSpine(FrappeTestCase):
	def _offloaded(self, *, folder, attached_to_doctype, is_folder=0, enabled=True,
				   file_url="/private/files/x.png"):
		"""True if after_insert decided to offload this file (real gate, offload() mocked)."""
		doc = frappe._dict(
			name="test-file",
			is_folder=is_folder,
			folder=folder,
			attached_to_doctype=attached_to_doctype,
			file_url=file_url,
		)
		with patch.object(file_events.blob_store, "is_enabled", return_value=enabled), \
				patch.object(file_events, "offload") as mock_offload:
			file_events.after_insert(doc)
			return mock_offload.called

	def test_home_attached_offloads(self):
		# The bug: a normal attachment lands in frappe's default "Home" folder. MUST offload now.
		self.assertTrue(self._offloaded(folder="Home", attached_to_doctype="FCRM Note"))

	def test_home_attachments_offloads(self):
		self.assertTrue(self._offloaded(folder="Home/Attachments", attached_to_doctype="CRM Lead"))

	def test_email_drafts_folder_offloads(self):
		# No draft special-case: a file in the old staging folder offloads too (on_trash reclaims it).
		self.assertTrue(self._offloaded(folder="Home/Email Drafts", attached_to_doctype=None))

	def test_unattached_offloads(self):
		self.assertTrue(self._offloaded(folder="Home", attached_to_doctype=None))

	def test_folder_row_never_offloads(self):
		# A folder (is_folder=1) is not a file — never offloaded (short-circuits before file_url).
		self.assertFalse(self._offloaded(folder="Home", attached_to_doctype=None, is_folder=1))

	def test_disabled_toggle_skips_offload(self):
		# Kill-switch honoured: toggle OFF -> nothing offloads (invariant A.6, code ships dormant).
		self.assertFalse(self._offloaded(folder="Home", attached_to_doctype="CRM Lead", enabled=False))

	# --- delete side: on_trash reclaims by Azure-proxy url + last-reference, NOT the custom flag,
	# so frappe core's add_attachments copies (custom=0) reclaim their blob too (no orphans). ---
	_PROXY = "/api/method/tatva_connect.storage.api.download_file?file_name=platform/x/ab_f.txt"

	def _reclaims(self, *, file_url, other_refs):
		"""True if on_trash deletes the blob for this file_url, given `other_refs` rows sharing it."""
		doc = frappe._dict(name="f", file_url=file_url)
		with patch.object(file_events.frappe.db, "count", return_value=other_refs), \
				patch.object(file_events, "BlobStore") as MockStore:
			file_events.on_trash(doc)
			return MockStore.return_value.delete.called

	def test_on_trash_reclaims_azure_file_on_last_reference(self):
		# No custom flag on the doc at all — reclaim is decided by the proxy url + last reference.
		self.assertTrue(self._reclaims(file_url=self._PROXY, other_refs=0))

	def test_on_trash_keeps_blob_while_another_row_shares_it(self):
		# Shared blob (draft/original, or sent-copy/original): keep until the LAST reference goes.
		self.assertFalse(self._reclaims(file_url=self._PROXY, other_refs=0 + 1))

	def test_on_trash_ignores_local_file(self):
		# A non-proxy (local) url yields no blob key -> no Azure op at all.
		self.assertFalse(self._reclaims(file_url="/private/files/x.png", other_refs=0))
