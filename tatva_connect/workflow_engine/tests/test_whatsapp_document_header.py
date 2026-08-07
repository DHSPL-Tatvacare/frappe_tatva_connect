# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE DOCUMENT A PATIENT RECEIVES — the URL that goes on the wire must not expire, and must not be private.

A media header is an ORDINARY NAMED PARAMETER (D6). The provider matches parameters by name, so a document
header is `{"name": "pdfLink", "value": "<url>"}` beside every other blank the template asks for — which is
why `transport.send_template_message` is untouched by this feature and why this suite asserts on the request
BODY rather than on a second send path that does not exist.

TWO WAYS TO HAND OVER A URL, AND ONLY ONE OF THEM ARRIVES
---------------------------------------------------------
`file_manager.fetch_url` answers with a SHORT-LIVED SIGNED link — `sas_ttl_seconds`, 900 by default. It is
the right answer for a caller who downloads the bytes now, and the wrong one here: the provider STORES what
we hand it and fetches it whenever it likes, so a signed link is a document that arrives dead on a patient's
phone with nothing in our logs to say so. The stable proxy URL (`storage/api.download_file`) never expires
and mints a fresh SAS per request behind itself, which is exactly the difference. So the assertions are that
the value carries NO `sig=` and NO `se=`, and that its only query parameter is the blob key.

A PRIVATE FILE IS A ROUTING ANSWER, NOT AN EXCEPTION
-----------------------------------------------------
`download_file` gates a private file on `File.is_downloadable()`, and the provider fetches as a stranger with
no session of ours — the same fact `wati.send_media` records when it says a URL send of our own file would
fail. A document the provider cannot fetch means one thing, "the message did not reach the patient", and that
is precisely what the author's `failed` edge is for. Killing the journey over it would kill it for every
patient after this one. A file that is GONE reads the same way. Half a header — a placeholder with no file,
or a file with no placeholder — is the other side of the split: no patient's data can produce it, it is
wrong for every record equally, so it raises.

WHAT IS ASSERTED, AND WHERE IT IS READ FROM
--------------------------------------------
`transport._post` is the last function before the socket, and the dict it receives IS the request body. Every
positive assertion here reads that dict, through the REAL WATI adapter — never a mock being called, and never
a parameter list rebuilt by the test.

NOTHING IS ARMED, UPLOADED OR LEFT BEHIND
------------------------------------------
The two operator switches are patched in-process; the sends switch ships OFF and this suite never writes it.
No blob is ever created: the fixture rows point at a key `BlobStore.new_key` minted and nothing uploaded,
which is precisely what `file_manager.link` is for — so there is no Azure object to clean up, which is the
one thing `FrappeTestCase`'s rollback could not do for us.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.workflow_engine.tests.test_whatsapp_document_header
"""
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions, sends
from tatva_connect.storage import blob_store, file_manager
from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.whatsapp import channel, routing, transport, wati
from tatva_connect.workflow_engine import refs
from tatva_connect.tatva_connect.doctype.crm_campaign_document.crm_campaign_document import (
	DT as CAMPAIGN_DT,
)
from tatva_connect.workflow_engine.tests import fixtures as fx

_ACCOUNT = "document-header-probe-account"
_TEMPLATE = "document-header-probe"
# The template's own blank, so the header can be proven to arrive BESIDE it rather than instead of it.
_SLOT = "patient_name"
_FILLED = "Probe"
# WATI's own worked example of a document-header placeholder. The name is the author's to type; nothing infers it.
_HEADER = "pdfLink"
# The same unassigned number the number-format suite uses — a shape, never a subscriber.
_NUMBER = "+91-9876543210"
_REF = "gen.document_file"


class _Harness(FrappeTestCase):
	"""One real lead, one real account, one real template, two real File rows, and the real send path.

	The WATI adapter is NOT doubled: the account resolves to it and `send_template` -> `transport` runs for
	real. The one vendor answer patched is `template_variables`, which reads a catalogue column mirrored
	from the provider that a probe template has no honest value for — and what this suite is about starts
	after that list is known.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		cls.account = fx.whatsapp_account(_ACCOUNT)
		cls.template = fx.whatsapp_template(_TEMPLATE, cls.account)
		cls.lead = fx.make_lead(mobile_no=_NUMBER)
		cls.campaign = frappe.get_doc({
			"doctype": CAMPAIGN_DT, "lead": cls.lead.name,
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		cls.public_pdf, cls.public_url = cls._document(0)
		cls.private_pdf, _private_url = cls._document(1)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		for name in (cls.public_pdf, cls.private_pdf):
			frappe.delete_doc("File", name, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.delete_doc(CAMPAIGN_DT, cls.campaign.name, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.delete_doc("WhatsApp Templates", cls.template, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.delete_doc("WhatsApp Account", _ACCOUNT, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _document(cls, is_private):
		"""A File row in the state an OFFLOADED document really carries. Returns `(name, absolute proxy url)`.

		`BlobStore.new_key` mints the key and `file_manager.link` files the row — the two front doors, so the
		fixture cannot drift from what the storage layer really writes, and neither of them uploads anything.

		`is_private` is written with `db.set_value` because the checkpoint (`file_events.may_be_public`) reads
		the operator's ALLOWLIST, which is bench config no test may write and which `FileOverride.validate`
		re-applies on every save. Which owner earns a public file is that checkpoint's decision and the
		campaign-document suite's subject; this suite is about what the send path does with the flag it finds.
		"""
		key = blob_store.BlobStore.new_key("campaign-document-probe.pdf", CAMPAIGN_DT, cls.campaign.name)
		row = file_manager.link(
			blob_store.download_url(key),
			attached_to_doctype=CAMPAIGN_DT,
			attached_to_name=cls.campaign.name,
			meta={"file_name": "campaign-document-probe.pdf"},
		)
		frappe.db.set_value("File", row.name, "is_private", is_private, update_modified=False)
		return row.name, frappe.utils.get_url(blob_store.download_url(key))

	def _send(self, context=None, **header):
		"""The real send path to the point where it queues. Returns `(output, marker_or_thunk, enqueued)`.

		`frappe.enqueue` is captured rather than mocked away, so what would have been handed to the delivery
		job is an outcome this suite can read. Nothing is enqueued and no provider is reached here.
		"""
		enqueued = {}
		with patch.object(sends, "sends_enabled", return_value=True), \
		     patch.object(routing, "resolve_account_for_lead", return_value=self.account), \
		     patch.object(channel, "is_enabled", return_value=True), \
		     patch.object(wati, "template_variables", return_value=[_SLOT]), \
		     patch.object(frappe, "enqueue", lambda _method, **kw: enqueued.update(kw)):
			output, deferred = sends.send_whatsapp(
				self.lead.name, "crm_lead.mobile_no", self.template,
				context={"crm_lead.mobile_no": _NUMBER, **(context or {})},
				values=[{"name": _SLOT, "mode": refs.LITERAL, "value": _FILLED}],
				**header,
			)
			if callable(deferred):
				deferred()
		return output, deferred, enqueued

	def _on_the_wire(self, enqueued):
		"""The body that would REALLY be posted, through the real adapter. `transport._post` is the last
		function before the socket, and the dict it is handed is the request body itself."""
		self.assertTrue(enqueued, "nothing was queued, so there is no request body to read")
		posted = {}

		def _capture(_url, _token, body):
			posted.update(body)
			return {"result": True, "message": {"localMessageId": "document-header-probe"}}

		with patch.object(transport, "_post", _capture):
			sends._deliver_whatsapp(**enqueued)
		return posted

	def _header_value(self, body):
		"""The document header's value out of the request body, or fail saying what was really sent."""
		for parameter in body.get("parameters") or []:
			if parameter.get("name") == _HEADER:
				return parameter.get("value")
		raise AssertionError(f"no {_HEADER} parameter on the wire — the body carried {body.get('parameters')}")


class TestTheDocumentReachesTheWire(_Harness):
	"""The positive path, asserted on the request body and never on a call."""

	def test_the_documents_url_goes_out_as_the_named_parameter(self):
		"""THE headline. A media header is a named parameter, so a send that does not append one puts the
		patient's document nowhere — the message arrives with an empty header and nothing says why."""
		output, _thunk, enqueued = self._send(
			{_REF: self.public_pdf}, document_variable=_HEADER, document_file=_REF,
		)
		body = self._on_the_wire(enqueued)

		self.assertEqual(output, sends.SENT)
		self.assertIn(
			{"name": _HEADER, "value": self.public_url}, body["parameters"],
			"the document header never reached the request body",
		)

	def test_the_header_is_appended_beside_the_templates_own_blanks(self):
		"""It is an EXTRA parameter, not a replacement. A header that overwrote the parameter list would
		send the document with every other blank empty, which is the defect `_filled_rows` exists to stop."""
		_output, _thunk, enqueued = self._send(
			{_REF: self.public_pdf}, document_variable=_HEADER, document_file=_REF,
		)
		body = self._on_the_wire(enqueued)

		self.assertIn({"name": _SLOT, "value": _FILLED}, body["parameters"])
		self.assertEqual(len(body["parameters"]), 2, f"the wire carried {body['parameters']}")

	def test_the_url_is_absolute(self):
		"""The proxy URL is stored root-relative, and a root-relative URL means nothing to a provider on
		another host: it would fetch its own domain and get a 404, or nothing at all."""
		_output, _thunk, enqueued = self._send(
			{_REF: self.public_pdf}, document_variable=_HEADER, document_file=_REF,
		)
		value = self._header_value(self._on_the_wire(enqueued))

		self.assertTrue(value.startswith(frappe.utils.get_url()), f"the provider was handed {value}")

	def test_the_url_carries_no_sas_token(self):
		"""THE red this suite exists for. `file_manager.fetch_url` is the obvious call and the wrong one:
		its link expires in minutes while the provider keeps ours and fetches it later. A SAS is recognised
		by its own query — `sig=` and `se=` — and the stable route carries neither, only the blob key."""
		_output, _thunk, enqueued = self._send(
			{_REF: self.public_pdf}, document_variable=_HEADER, document_file=_REF,
		)
		value = self._header_value(self._on_the_wire(enqueued))

		self.assertNotIn("sig=", value, "a signed link was handed to the provider — it dies before it is fetched")
		self.assertNotIn("se=", value, "a link with an expiry was handed to the provider")
		self.assertEqual(
			set(parse_qs(urlparse(value).query)), {blob_store.QUERY_KEY},
			"the stable proxy URL carries the blob key and nothing else",
		)

	def test_the_url_is_the_permission_gated_proxy_route(self):
		"""Which route it is matters as much as which token it lacks: the proxy is what mints a FRESH SAS
		per request behind itself, so the link keeps working for as long as the file exists."""
		_output, _thunk, enqueued = self._send(
			{_REF: self.public_pdf}, document_variable=_HEADER, document_file=_REF,
		)

		self.assertIn(blob_store.DOWNLOAD_METHOD, self._header_value(self._on_the_wire(enqueued)))

	def test_a_message_with_no_document_is_exactly_what_it_always_was(self):
		"""Blank means no document header, and a send that carries none must be byte-identical to every
		send this app made before the controls existed — the controls are always present, so most sends
		leave them empty."""
		_output, _thunk, enqueued = self._send()
		body = self._on_the_wire(enqueued)

		self.assertEqual(body["parameters"], [{"name": _SLOT, "value": _FILLED}])


class TestAFileTheProviderCannotFetchIsARoutingAnswer(_Harness):
	"""Three data states, three `failed` edges, nothing queued — and three different sentences, because an
	author reading the step log has to know which one to fix."""

	def test_a_private_document_takes_the_failed_edge_and_queues_nothing(self):
		"""The provider fetches with no session of ours, so a private file is a 403 on the patient's phone.
		Sending anyway would deliver a message whose header is a broken attachment."""
		output, marker, enqueued = self._send(
			{_REF: self.private_pdf}, document_variable=_HEADER, document_file=_REF,
		)

		self.assertEqual(output, sends.FAILED)
		self.assertEqual(enqueued, {}, "a document the provider cannot fetch was queued anyway")
		self.assertIn(self.private_pdf, marker, "the step log must name the document that could not be sent")
		self.assertIn("private", marker)

	def test_a_document_that_no_longer_exists_takes_the_failed_edge(self):
		"""A journey can park for days between the render and the send, and a lead deleted in between takes
		its documents with it (M1). The send must refuse rather than post a dead link."""
		output, marker, enqueued = self._send(
			{_REF: "a-file-that-was-deleted"}, document_variable=_HEADER, document_file=_REF,
		)

		self.assertEqual(output, sends.FAILED)
		self.assertEqual(enqueued, {})
		self.assertIn("a-file-that-was-deleted", marker)

	def test_a_reference_that_resolves_to_nothing_takes_the_failed_edge(self):
		"""The upstream render failed, so nothing produced the file. That is a state of this patient's run,
		not a broken workflow — the same answer a recipient that resolves to nothing already gets."""
		output, marker, enqueued = self._send(document_variable=_HEADER, document_file=_REF)

		self.assertEqual(output, sends.FAILED)
		self.assertEqual(enqueued, {})
		self.assertIn(_REF, marker, "the refusal must name the control the author has to fix")

	def test_the_three_refusals_read_differently(self):
		"""One `failed` edge, three sentences. If they collapse into one, the step log stops telling an
		author whether to fix the graph, the allowlist, or nothing at all."""
		markers = {
			self._send({_REF: self.private_pdf}, document_variable=_HEADER, document_file=_REF)[1],
			self._send({_REF: "a-file-that-was-deleted"}, document_variable=_HEADER, document_file=_REF)[1],
			self._send(document_variable=_HEADER, document_file=_REF)[1],
		}

		self.assertEqual(len(markers), 3, f"two data states report the same thing: {markers}")


class TestHalfADocumentHeaderIsAuthorError(_Harness):
	"""The other side of the split. No patient's data can produce a half-filled header, and it is wrong for
	every record equally — so it raises rather than telling the author "the patient did not get it"."""

	def test_a_half_filled_header_raises_instead_of_routing(self):
		halves = {
			"a placeholder with no file": {"document_variable": _HEADER},
			"a file with no placeholder": {"document_file": _REF},
		}
		for described, header in halves.items():
			with self.subTest(described), self.assertRaises(ValueError):
				self._send({_REF: self.public_pdf}, **header)


class TestTheNodeHandsItsControlsToTheSender(_Harness):
	"""A declared control that the handler drops is a feature that exists only on the screen."""

	def test_the_node_forwards_both_controls_to_the_sender(self):
		"""Driven through `_action_send_whatsapp`, the function the interpreter really calls, so a control
		declared on the verb and lost on the way to the sender cannot ship green."""
		params = frappe._dict({
			"action_type": "Send WhatsApp", "whatsapp_template": self.template,
			"contact_number": "crm_lead.mobile_no",
			"template_values": [{"name": _SLOT, "mode": refs.LITERAL, "value": _FILLED}],
			"document_variable": _HEADER, "document_file": _REF,
		})
		context = {"crm_lead.mobile_no": _NUMBER, _REF: self.public_pdf}
		enqueued = {}
		with patch.object(sends, "sends_enabled", return_value=True), \
		     patch.object(routing, "resolve_account_for_lead", return_value=self.account), \
		     patch.object(channel, "is_enabled", return_value=True), \
		     patch.object(wati, "template_variables", return_value=[_SLOT]), \
		     patch.object(frappe, "enqueue", lambda _method, **kw: enqueued.update(kw)):
			deferred = actions._action_send_whatsapp(params, self.lead.name, context, None, None)
			if callable(deferred):
				deferred()

		self.assertEqual(context.get(refs.OUTPUT), sends.SENT)
		self.assertIn(
			{"name": _HEADER, "value": self.public_url}, self._on_the_wire(enqueued)["parameters"],
			"the node's two controls never reached the sender",
		)
