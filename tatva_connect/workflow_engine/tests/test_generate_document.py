# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""GENERATE DOCUMENT (W13, plan §4) — the template really becomes a PDF, the node is really waitable, and
the render is dispatched where it cannot starve the engine.

Four claims are held here, and each one is a defect this chain would otherwise ship silently.

REAL BYTES, NEVER A MOCK. A patched `get_pdf` would keep every test below green while wkhtmltopdf was
missing, misconfigured or hung — which is precisely the class of failure a render has. So the template is
authored, rendered and turned into bytes for real, and the assertion is on the `%PDF` header. It needs NO
network: the markup carries no remote asset, which is the same rule plan D10 puts on a real template
(wkhtmltopdf fetches every `<img src>` during the render, with no session and no timeout of its own).

WAITABLE, WHICH CALL API IS NOT. `outcomes_of` is what a Wait's "Waiting on" list is built from, so a node
with no outcomes can never be waited on however many outputs it declares. Call API is asserted in the same
test as the CONTROL — without it, "returns two outcomes" would pass against a function that returned two
outcomes for everything.

THE `long` LANE, NEVER `workflow`. `workflow` is the lane the engine walks graphs on, and the obvious copy
of `_deliver_voice` (which correctly uses `workflow` for a sub-second API call) would put seconds of
subprocess work in front of every journey on the bench.

POINTERS, NEVER A URL. The outcome payload names the `CRM Campaign Document` and the `File`. A URL captured
into journey state was true once — the journey parks for days, and the file's real address is derived at
SEND time from the File row (I5, plan D5).

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_generate_document
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions
from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import document_render, refs
from tatva_connect.workflow_engine.tests import fixtures as fx

_NODE = "doc1"
_TOKEN = "generate-document-probe::doc1"
_VERB = "Generate Document"

# An AI-shaped payload: a literal the author typed, and a value an upstream Call API captured into state.
_PATIENT = "WF Probe"
_SUMMARY = "Three readings logged this fortnight, all within range."
_NEXT_STEP = "Repeat the panel before the next review."


def _web_template(name):
	"""A `Web Template` that DECLARES its inputs — the whole reason plan D1 picked this doctype over one of
	ours. Non-standard on purpose: `before_save` exports a standard template into an app directory on a
	developer bench, so a test that ticked it would write files into the tree it is testing."""
	if frappe.db.exists("Web Template", name):
		frappe.delete_doc("Web Template", name, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "Web Template",
		"name": name,
		"type": "Section",
		"standard": 0,
		# Table-and-float markup with NO remote asset: this is what makes the render local and bounded.
		"template": (
			"<h1>{{ patient_name }}</h1>"
			"<table><tr><td>{{ summary }}</td></tr><tr><td>{{ next_step }}</td></tr></table>"
		),
		"fields": [
			{"label": "Patient Name", "fieldname": "patient_name", "fieldtype": "Data"},
			{"label": "Summary", "fieldname": "summary", "fieldtype": "Text"},
			{"label": "Next Step", "fieldname": "next_step", "fieldtype": "Data"},
		],
	}).insert(ignore_permissions=True).name  # authz-ok: tier-c — test fixture, no user input


def _document_values():
	"""The author's rows: one Literal, one From Context reading what an upstream node captured. The mixed
	pair matters — a filler that only ever saw literals would drop the AI payload and nothing would say so."""
	return [
		{"name": "patient_name", "mode": refs.LITERAL, "value": _PATIENT},
		{"name": "summary", "mode": refs.FROM_CONTEXT, "value": "call.summary"},
		{"name": "next_step", "mode": refs.LITERAL, "value": _NEXT_STEP},
	]


def _state():
	"""Journey state as the interpreter hands it to a handler: the engine's token for THIS node, plus an
	upstream node's captured bucket. Returns `(values, view)` — the handler writes through the view."""
	values = refs.Values(buckets={"call": {"summary": _SUMMARY}})
	values[refs.TOKEN] = _TOKEN
	return values, values.writing_as(_NODE)


def _action(template, **overrides):
	"""The node's config as `_run_verb` builds it — `action_type` is set because `resolve_target` reads it."""
	params = frappe._dict({
		"action_type": _VERB,
		"document_template": template,
		"document_values": _document_values(),
	})
	params.update(overrides)
	return params


class TestTheTemplateReallyBecomesAPdf(FrappeTestCase):
	"""THE test that cannot be faked. Every other test in this file could pass with the PDF toolchain
	entirely absent; this one renders an authored template with a payload and asserts on the bytes.

	NO NETWORK IS REACHED. The markup embeds no image, no stylesheet and no font, so wkhtmltopdf resolves
	nothing — the same property plan D10 demands of a production template, asserted here as a floor.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.template = _web_template("generate-document-render-probe")

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("Web Template", cls.template, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def test_the_node_offers_exactly_the_inputs_the_template_declares(self):
		"""The slots brain. The grid, the publish gate and the runtime filler all read `document_template_slots`
		— a second reader (a regex over the markup, the tempting shortcut) would drift from the renderer the
		first time a template used a block, and the gate would then check a shape nobody fills."""
		self.assertEqual(
			actions.document_template_slots(self.template),
			["patient_name", "summary", "next_step"],
			"the offered rows must be the template's OWN declared fields, in its own order",
		)

	def test_a_template_rendered_with_a_payload_carries_every_value(self):
		"""Rendered through the `Web Template`'s own `render`, which is what `render_document` calls. A second
		renderer here would prove nothing about the one that ships."""
		html = frappe.get_doc("Web Template", self.template).render({
			"patient_name": _PATIENT, "summary": _SUMMARY, "next_step": _NEXT_STEP,
		})

		self.assertIn(_PATIENT, html)
		self.assertIn(_SUMMARY, html, "the captured value never reached the document")
		self.assertIn(_NEXT_STEP, html)
		self.assertNotIn("{{", html, "an unrendered placeholder is a hole in a document a patient reads")

	def test_the_html_becomes_real_pdf_bytes(self):
		"""`_render_pdf` is the ONE seam our code has onto the PDF toolchain, so it is the thing asserted.
		`%PDF` is the format's own magic number: bytes that do not start with it are not a document, and a
		byte count guards the other direction — a header with an empty body still opens in nothing."""
		html = frappe.get_doc("Web Template", self.template).render({
			"patient_name": _PATIENT, "summary": _SUMMARY, "next_step": _NEXT_STEP,
		})

		pdf = document_render._render_pdf(html)

		self.assertIsInstance(pdf, bytes, "a PDF is bytes; a str here means nothing can be filed or sent")
		self.assertTrue(pdf.startswith(b"%PDF"), "the render produced something that is not a PDF at all")
		self.assertGreater(len(pdf), 1000, f"{len(pdf)} bytes is a header with no document under it")


class TestTheNodeIsWaitable(FrappeTestCase):
	"""The single most important difference from Call API, and the reason the outcomes are declared STATIC:
	a render is answered by our own job rather than by a channel's adapters, so there is no channel to
	derive the list from."""

	def test_a_wait_after_the_node_offers_the_two_document_outcomes(self):
		"""`outcomes_of` IS the Wait's "Waiting on" list. Call API is the CONTROL in the same test: it declares
		outputs and no outcomes, so nothing can ever wait on one — without that half, this test would pass
		against a function that answered two outcomes for every verb alive."""
		self.assertEqual(
			actions.outcomes_of(_VERB),
			[document_render.DOCUMENT_READY, document_render.DOCUMENT_FAILED],
		)
		self.assertEqual(
			actions.outcomes_of("Call API"), [],
			"Call API answers synchronously and declares no outcomes — if this list grew, the assertion above "
			"is measuring nothing",
		)

	def test_the_outcome_names_are_read_from_the_job_that_delivers_them(self):
		"""ONE brain. The names the Wait offers and the names `render_document` delivers are the same two
		objects, so a rename cannot leave a journey parked on a signal nothing will ever send."""
		self.assertIs(actions.VERBS[_VERB]["outcomes"][0], document_render.DOCUMENT_READY)
		self.assertIs(actions.VERBS[_VERB]["outcomes"][1], document_render.DOCUMENT_FAILED)
		self.assertNotIn("outcomes_channel", actions.VERBS[_VERB],
		                 "a render has no channel to derive outcomes from")


class TestTheRenderIsDispatchedOnTheLongLane(FrappeTestCase):
	"""The armed path. The switch is patched ON — the DB row is never touched — and `frappe.enqueue` is
	patched so nothing is really queued; what is asserted is WHERE the work was sent and WITH WHAT."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		cls.template = _web_template("generate-document-dispatch-probe")
		cls.lead = fx.make_lead()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Web Template", cls.template, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def _dispatch(self, **overrides):
		with patch("tatva_connect.workflow_engine.document_render.render_enabled", return_value=True), \
		     patch("frappe.enqueue") as enqueue:
			values, view = _state()
			marker = actions._action_generate_document(
				_action(self.template, **overrides), self.lead.name, view, fx.AXES, None,
			)
		return values, marker, enqueue

	def test_the_render_goes_to_long_after_commit_and_carries_its_own_death_penalty(self):
		"""THE lane assertion. `workflow` is where the engine walks graphs; a render is seconds of subprocess
		work, so a queued render sitting in that lane is a starved engine — and `_deliver_voice`, the shape
		this handler is modelled on, correctly uses `workflow` for a sub-second API call. Copying it here is
		the mistake this test exists to catch.

		`enqueue_after_commit` is the other half: a segment that rolls back must render nothing (the data-loss
		rule the start enqueue already records). And the job carries `timeout=` because pdfkit has NO timeout
		of its own — without it one dead asset host holds the worker for ever.
		"""
		_values, _marker, enqueue = self._dispatch()

		enqueue.assert_called_once()
		args, kwargs = enqueue.call_args
		self.assertEqual(args[0], "tatva_connect.workflow_engine.document_render.render_document")
		self.assertEqual(kwargs.get("queue"), "long")
		self.assertNotEqual(kwargs.get("queue"), "workflow", "a render must never occupy the engine's own lane")
		self.assertTrue(kwargs.get("enqueue_after_commit"),
		                "a rolled-back segment would otherwise still render and file a document")
		self.assertEqual(kwargs.get("timeout"), document_render.RENDER_TIMEOUT_SECONDS)

	def test_the_job_is_told_which_journey_to_wake_and_what_to_render(self):
		"""Correlation is the engine's token for THIS node, so the ready signal wakes THAT journey — a lead can
		be in two journeys at once, and correlating on the lead alone wakes an arbitrary one. The resolved
		values ride with it: a job handed the author's rows instead of the filled map would resolve them in a
		worker that has no journey state and render blanks."""
		_values, _marker, enqueue = self._dispatch(file_name="care-plan")
		kwargs = enqueue.call_args[1]

		self.assertEqual(kwargs.get("correlation"), _TOKEN)
		self.assertEqual(kwargs.get("subject_doctype"), "CRM Lead")
		self.assertEqual(kwargs.get("subject_name"), self.lead.name)
		self.assertEqual(kwargs.get("file_name"), "care-plan")
		self.assertEqual(kwargs.get("values"), {
			"patient_name": _PATIENT, "summary": _SUMMARY, "next_step": _NEXT_STEP,
		}, "the captured value must reach the job already resolved against journey state")

	def test_the_node_leaves_by_queued_and_emits_the_row_it_created(self):
		"""`queued` is the honest synchronous answer — the render was accepted, not that it worked. The row it
		names is a REAL record here, not a mock: the enqueue is patched, the insert is not."""
		values, marker, enqueue = self._dispatch()

		self.assertEqual(values.get(refs.OUTPUT), "queued")
		emitted = values.get(f"{_NODE}.campaign_document")
		self.assertTrue(frappe.db.exists(document_render.CAMPAIGN_DOCUMENT_DT, emitted),
		                "the node emitted a pointer to a record that does not exist")
		self.assertEqual(
			frappe.db.get_value(document_render.CAMPAIGN_DOCUMENT_DT, emitted, "status"),
			document_render.QUEUED,
			"a row born in any other state claims an answer nobody has yet",
		)
		self.assertEqual(emitted, enqueue.call_args[1].get("campaign_document"),
		                 "the journey and the job must be pointed at the SAME row")
		self.assertIn(emitted, marker, "the step log must say which document was queued")
		self.assertIsNone(values.get(f"{_NODE}.document_file"),
		                  "the file is not knowable yet — the render reports it on the outcome")


class TestADormantSwitchRendersNothing(FrappeTestCase):
	"""D9, the standing bible: nothing arms itself. Off, the node takes its `failed` edge exactly as the
	voice channel does — no row, no job, no PDF. This test never goes red."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		cls.template = _web_template("generate-document-dormant-probe")
		cls.lead = fx.make_lead()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Web Template", cls.template, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def test_the_render_switch_is_off_on_this_bench(self):
		"""It SHIPS off. A bench that finds it armed has had it turned on by something, and every "nothing
		rendered" claim after that is unfalsifiable."""
		self.assertFalse(document_render.render_enabled(),
		                 "Document::Generation::render is armed — it ships OFF")

	def test_a_dormant_node_takes_failed_and_queues_nothing(self):
		"""The gate is in the HANDLER, not in the job (D9): a dormant bench must dispatch no work at all. The
		outcomes are three, and all three are asserted because any one of them alone can be true while the
		chain still leaks — an edge with a queued job behind it, or a row written for a render that will never
		happen and sits in `Queued` for ever."""
		before = frappe.db.count(document_render.CAMPAIGN_DOCUMENT_DT, {"lead": self.lead.name})

		with patch("frappe.enqueue") as enqueue:
			values, view = _state()
			marker = actions._action_generate_document(
				_action(self.template), self.lead.name, view, fx.AXES, None,
			)

		self.assertEqual(values.get(refs.OUTPUT), "failed")
		self.assertIn("switched off", marker, "the author must be told WHY the failed edge was taken")
		enqueue.assert_not_called()
		self.assertEqual(
			frappe.db.count(document_render.CAMPAIGN_DOCUMENT_DT, {"lead": self.lead.name}), before,
			"a dormant node wrote a campaign document nothing will ever render",
		)


class TestTheOutcomeCarriesPointersAndNeverAUrl(FrappeTestCase):
	"""The render, end to end and for real: template -> PDF bytes -> a File the campaign document OWNS ->
	the signal a parked journey wakes on. Read off the durable `CRM Workflow Signal` row, which is the inbox
	the engine really consumes, rather than off a recorded call.

	The engine switch is armed because `deliver_signal` is dormant-by-default and inserts nothing while it is
	off — with it off this suite would be green having proven the render delivers no signal at all.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		fx.arm_engine(True, cls)
		cls.template = _web_template("generate-document-outcome-probe")
		cls.lead = fx.make_lead()
		cls.row = frappe.get_doc({
			"doctype": document_render.CAMPAIGN_DOCUMENT_DT,
			"lead": cls.lead.name,
			"template": cls.template,
			"status": document_render.QUEUED,
		}).insert(ignore_permissions=True).name  # authz-ok: tier-c — test fixture, no user input
		frappe.db.commit()

		document_render.render_document(
			cls.row, "CRM Lead", cls.lead.name,
			values={"patient_name": _PATIENT, "summary": _SUMMARY, "next_step": _NEXT_STEP},
			correlation=_TOKEN, file_name="care-plan",
		)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete(fx.SIGNAL_DT, {"correlation": _TOKEN})
		# Through `delete_doc`, and only through it: the cascade is what takes the campaign document and its
		# blob with the lead (M1). A bulk row delete would leave real bytes in the container.
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Web Template", cls.template, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def _signal(self):
		found = frappe.get_all(
			fx.SIGNAL_DT,
			filters={"correlation": _TOKEN, "event_name": document_render.DOCUMENT_READY},
			fields=["name", "subject_doctype", "subject_name", "payload_json"], limit=1,
		)
		self.assertTrue(found, "the render reported nothing, so a journey parked on it waits for ever")
		return found[0]

	def test_the_render_files_a_real_pdf_on_the_campaign_document(self):
		"""The bytes have to land somewhere a patient can be sent from, owned by the row that makes them
		publishable without publishing a lead's clinical attachments (D4)."""
		row = frappe.get_doc(document_render.CAMPAIGN_DOCUMENT_DT, self.row)

		self.assertEqual(row.status, document_render.READY, f"the render failed: {row.error}")
		self.assertTrue(row.document_file, "a ready document that names no file is not a document")
		attachment = frappe.get_doc("File", row.document_file)
		self.assertEqual(attachment.attached_to_doctype, document_render.CAMPAIGN_DOCUMENT_DT)
		self.assertEqual(attachment.attached_to_name, self.row)
		self.assertTrue(attachment.file_name.endswith(".pdf"),
		                "a document that downloads with no extension opens in nothing")

	def test_the_outcome_names_the_document_and_the_file_as_pointers(self):
		"""Both keys are the ones the verb DECLARES it emits, and both resolve to records that really exist —
		a payload naming a row nobody can load is the same defect as no payload at all."""
		payload = frappe.parse_json(self._signal().payload_json)

		self.assertEqual(payload.get("campaign_document"), self.row)
		self.assertTrue(frappe.db.exists("File", payload.get("document_file")),
		                "the outcome pointed at a file that does not exist")
		self.assertEqual(
			payload.get("document_file"),
			frappe.db.get_value(document_render.CAMPAIGN_DOCUMENT_DT, self.row, "document_file"),
			"the row and the signal must name the SAME file",
		)

	def test_the_outcome_carries_no_url_of_any_kind(self):
		"""I5 / D5. A journey parks for days; a URL captured into state was true once, and a SAS one is dead
		on arrival. The address is derived at SEND time from the File row, by the node that needs it — so any
		URL-shaped value here is a second, staler answer to a question the File already answers."""
		payload = frappe.parse_json(self._signal().payload_json)

		for key, value in payload.items():
			with self.subTest(key=key):
				self.assertNotIn("://", str(value), f"{key} carries a URL into journey state")
				self.assertNotIn("/api/method", str(value), f"{key} carries a URL into journey state")
				self.assertNotIn("?sv=", str(value), f"{key} carries a SAS token, which expires")

	def test_the_signal_is_addressed_to_the_lead_and_correlated_to_the_node(self):
		"""Correlation is what makes the wake per-JOURNEY. Delivered without it, a lead in two journeys wakes
		an arbitrary one — which is the exact defect every other wake path in this app already avoids."""
		signal = self._signal()

		self.assertEqual(signal.subject_doctype, "CRM Lead")
		self.assertEqual(signal.subject_name, self.lead.name)
