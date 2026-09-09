"""Rebuild the Activity rail index so it holds saves, and no longer holds a file twice.

Two rules changed in `activity/timeline.py` and both are about which rows the index should hold:

  * a `Version` is now a source, so a save is one rail event and the rail can page past frappe's
    ten-version window instead of reading it fresh at every request;
  * a File that arrived on a message, a comment, a note or a task is no longer its own event — each of
    those renders its own attachments, so the file and the thing it belongs to were drawn as two rows.

An index built under the old rules is missing the first and holds the second. `timeline.rebuild()` is the
repair path the table was designed around: it clears a lead before regenerating it, so this drops the
stale file pointers and writes the version pointers in one pass, and re-running it changes nothing.

NOTHING IS LOST. The index is derived — every row is a pointer regenerated from the source document, and
no source row is read, written or deleted here. A file that stops being its own rail event is still on the
Attachments tab, which lists every document a lead holds whatever it arrived on.

ONLY WHERE THE INDEX IS IN USE. `Activity::Timeline::indexing` ships dormant; while it is off the rail
serves from the read-time merge and this table is empty, so there is nothing to repair and the activator
will build it correctly whenever an operator switches it on. Enqueued, never inline: a migrate must not
block on every lead on the site, and `build_all` commits per chunk of 500 and logs-and-continues on a
lead that fails, so one bad row cannot take the pass down.

No schema_setup twin: this is a data rebuild, not a structure. A fresh site has no leads to index and the
toggle's activator owns the first fill.
"""

import frappe

from tatva_connect.activity import timeline
from tatva_connect.automation.settings import is_enabled


def execute():
	if not is_enabled(timeline.TOGGLE):
		frappe.logger().info("CRM Timeline Event: indexing is off, nothing to reindex")
		return

	frappe.enqueue(
		"tatva_connect.activity.timeline.build_all",
		queue="long",
		timeout=14400,
		enqueue_after_commit=True,
	)
