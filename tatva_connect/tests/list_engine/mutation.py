# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Planted defects — the negative controls that prove the list-engine suite can go RED.

Mirrors `tests/authz/mutation.py`. A green suite proves nothing unless it is shown to catch a real bug,
so every capability this layer claims gets a deliberate defect planted into the PRODUCTION code, and
`test_self_validation.py` asserts the matching test module goes red. A plant that stays green is a False
Negative — the suite is blind there, and the build fails naming it.

WHY CODE MUTATIONS AND NOT DATA. The authz suite plants data (a share, a role grant) because its subject
IS data. This layer's subject is a TRANSLATION — a declaration becoming a query and a value — so its
defects live in code. Each plant is therefore `(module, attribute, replacement)`, applied around one
test run and restored in a `finally`, never left behind.

EVERY PLANT IS A DEFECT THIS LAYER HAS REALLY HAD, OR ONE CLASS OF THEM. The 2026-07-31 audit found six
severe defects and the operator found a seventh on screen the next day; those are replayed here verbatim
so the suite can never be blind to them twice. Where a plant is a class rather than a specific incident,
`english` says so.

The record shape is the authz one, minus the attack taxonomy which does not apply:

    id                                  stable name, used in the failure message
    english                             what a rep would experience if this shipped
    target                              "module:attribute" that gets replaced
    make                                () -> replacement, built lazily so imports stay cheap
    detector                            the test module that MUST go red
    untestable_without_browser          a reason string, or None
"""

from tatva_connect.list_engine import derived, engine, repair

# The test modules a plant can be scored against. Named here so a typo is a load error, not a silent pass.
TRANSLATION = "tatva_connect.tests.list_engine.test_engine_translation"
PROMISE = "tatva_connect.tests.list_engine.test_list_engine"
EDGES = "tatva_connect.tests.list_engine.test_edge_repairs"
HEAD = "tatva_connect.tests.list_engine.test_derived_head"
EXPORT = "tatva_connect.tests.list_engine.test_list_export"
VERIFIER = "tatva_connect.tests.list_engine.test_derived_fields"

# --- the replacements ------------------------------------------------------------------------------
# Each returns a callable with the SAME signature as the thing it replaces, differing by one decision.


def _bound_off_by_one():
	"""`>=` read as `>`: a record sitting exactly on a bucket's boundary falls out of it.

	The class of defect this whole layer exists to prevent — the two readers disagreeing on one instant."""
	original = derived.resolve

	def resolve(field, bucket, snap=None):
		terms = original(field, bucket, snap)
		return [[t[0], t[1], ">" if t[2] == ">=" else t[2], t[3]] for t in terms]

	return resolve


def _operand_untyped():
	"""The operand goes back to a bare string — REPLAYS the defect measured 2026-08-01.

	Untyped, `frappe.utils.data.compare` cannot name the fieldtype for a STANDARD column, so a bucket on
	`creation` raised `TypeError` and could not be saved at all; and on any Datetime an inclusive `<=`
	bound made SQL drop the row sitting exactly on it while Python kept it."""
	original = derived.resolve

	def resolve(field, bucket, snap=None):
		return [
			[t[0], t[1], t[2], str(t[3]) if hasattr(t[3], "isoformat") else t[3]]
			for t in original(field, bucket, snap)
		]

	return resolve


def _first_bucket_wins_everything():
	"""Every record reads as the first declared bucket. The column lies; the filter does not."""

	def value_of(field, row, snap=None):
		return field.buckets[0].value if field.buckets else None

	return value_of


def _themes_dropped_from_descriptor():
	"""The descriptor stops carrying colours — REPLAYS the grey group-by headers found on screen
	2026-08-01, which 34 green tests did not catch."""
	original = derived.DerivedField.descriptor

	def descriptor(self):
		described = dict(original(self))
		described.pop("themes", None)
		return described

	return descriptor


def _janitor_disabled():
	"""A request naming a field the list no longer has is passed through untouched — REPLAYS audit
	finding #1: the rep is locked out of their own saved view."""

	def scrub(kwargs):
		return kwargs, []

	return scrub


def _saved_view_never_repaired():
	"""The request is scrubbed but the stored view is left broken, so it arrives broken again tomorrow —
	the half-fix the save gate really shipped before it was unified."""

	def repair_view(name, gone=None):
		return False

	return repair_view


def _union_resolves_identifiers_again():
	"""The pre-2026-08-01 body: a query per bucket, every matching id collected into one `IN (…)`.

	Correct, and the reason the scale wall existed. The suite must notice the identifiers came back."""

	def union(field, values, snap=None):
		return None

	return union


def _kanban_fields_not_restored():
	"""A derived name is stripped for the query and never put back — REPLAYS audit finding #4: the card
	badge never draws and the rep's saved choice is overwritten server-side."""

	def _restore_listed(self, result, key, field):
		return None

	return _restore_listed


def _boot_version_frozen():
	"""The declaration version stops moving, so a rep's cached field menus never retire — REPLAYS audit
	finding #2: an authored field reaches nobody who has used the app before."""

	def declaration_version():
		return derived.EMPTY_VERSION

	return declaration_version


def _proof_skipped_on_save():
	"""`verify()` stops running at Save, so a declaration whose column and filter disagree can be stored
	— the silent-wrong class the doctype exists to refuse."""

	def verify(field, defaults=None, snap=None):
		return []

	return verify


def _colours_unchecked():
	"""A colour no badge can wear is accepted at Save and draws an unstyled pill on every surface."""
	original = derived._validate

	def _validate(field):
		for bucket in field.buckets:
			bucket.theme = bucket.theme if bucket.theme in derived.THEMES else None
		return original(field)

	return _validate


def _one_line_values_unchecked():
	"""A bucket value carrying a line break is accepted, and splits into two phantom menu entries."""
	original = derived.Bucket.__init__

	def __init__(self, value, filters, theme=None):
		original(self, str(value).replace("\n", "").replace("\t", "").strip(), filters, theme)

	return __init__


def _retirement_blocked():
	"""The proof runs on every save including a disable, so a field whose column has changed can no
	longer be switched off. The escape hatch grows a lock."""
	original = derived.verify

	def verify(field, defaults=None, snap=None):
		return original(field, defaults, snap)

	return verify


def _count_ignores_the_filter():
	"""The count stops carrying the page's narrowing, so the footer contradicts the screen (C7)."""
	original = engine.ListRequest._count

	def _count(self, terms):
		return original(self, [])

	return _count


def _calendar_window_ignored():
	"""The calendar goes back to asking for everything the filters match, whatever month is on screen."""

	def _window(self):
		return []

	return _window


# --- the catalogue ---------------------------------------------------------------------------------

MUTATIONS = [
	{
		"id": "MUT-boundary-off-by-one",
		"english": "a record sitting exactly on a bucket bound falls out of it — the two readers disagree",
		"target": "derived:resolve",
		"make": _bound_off_by_one,
		"detector": PROMISE,
		"untestable_without_browser": None,
	},
	{
		"id": "MUT-operand-untyped",
		"english": "the operand loses its column's type — `creation` becomes unusable and an inclusive bound splits the readers",
		"target": "derived:resolve",
		"make": _operand_untyped,
		"detector": VERIFIER,
		"untestable_without_browser": None,
	},
	{
		"id": "MUT-first-bucket-wins",
		"english": "every record displays as the first bucket while filters still return the right ones",
		"target": "derived:value_of",
		"make": _first_bucket_wins_everything,
		"detector": PROMISE,
		"untestable_without_browser": None,
	},
	{
		"id": "MUT-themes-dropped",
		"english": "colours stop reaching a surface — the grey group-by headers found on screen 2026-08-01",
		"target": "derived.DerivedField:descriptor",
		"make": _themes_dropped_from_descriptor,
		"detector": EDGES,
		"untestable_without_browser": None,
	},
	{
		"id": "MUT-janitor-off",
		"english": "a retired field locks the rep out of their own saved view (audit #1)",
		"target": "repair:scrub",
		"make": _janitor_disabled,
		"detector": EDGES,
		"untestable_without_browser": None,
	},
	{
		"id": "MUT-view-never-repaired",
		"english": "the request is fixed but the stored view is not, so it breaks again tomorrow",
		"target": "repair:repair_view",
		"make": _saved_view_never_repaired,
		"detector": EDGES,
		"untestable_without_browser": None,
	},
	{
		"id": "MUT-union-resolves-ids",
		"english": "the identifier walk returns — correct answers, table-sized queries (audit #5a)",
		"target": "derived:union",
		"make": _union_resolves_identifiers_again,
		"detector": EDGES,
		"untestable_without_browser": None,
	},
	{
		"id": "MUT-kanban-fields-lost",
		"english": "the card badge never draws and the rep's saved choice is reverted (audit #4)",
		"target": "engine.ListRequest:_restore_listed",
		"make": _kanban_fields_not_restored,
		"detector": EDGES,
		"untestable_without_browser": None,
	},
	{
		"id": "MUT-boot-version-frozen",
		"english": "an authored field never reaches a rep who used the app before (audit #2)",
		"target": "derived:declaration_version",
		"make": _boot_version_frozen,
		"detector": EDGES,
		"untestable_without_browser": None,
	},
	{
		"id": "MUT-proof-skipped",
		"english": "a declaration whose column and filter disagree can be saved",
		"target": "derived:verify",
		"make": _proof_skipped_on_save,
		"detector": HEAD,
		"untestable_without_browser": None,
	},
	{
		"id": "MUT-colours-unchecked",
		"english": "a colour no badge can wear is stored and draws an unstyled pill",
		"target": "derived:_validate",
		"make": _colours_unchecked,
		"detector": HEAD,
		"untestable_without_browser": (
			"cosmetic: an operator typo yields an unstyled pill, not a wrong answer. The refusal is a Desk-form validation and is tested directly by TestADeclarationThatCannotBeServedIsRefusedAtSave; scoring it here would pad recall with trivia and mask the defects that change what a rep is shown."
		),
	},
	{
		"id": "MUT-newline-values",
		"english": "a bucket value with a line break splits into two phantom menu entries",
		"target": "derived.Bucket:__init__",
		"make": _one_line_values_unchecked,
		"detector": HEAD,
		"untestable_without_browser": (
			"cosmetic: a line break in a bucket name yields a duplicate menu entry, not a wrong answer. Same reasoning as the colour plant — the Desk-form refusal is tested directly, and this does not belong in a scorecard about defects that reach a rep's data."
		),
	},
	{
		"id": "MUT-clock-read-twice",
		"english": "one response filters a record against one instant and displays it against another",
		"target": "derived:snapshot",
		"make": None,
		"detector": TRANSLATION,
		"untestable_without_browser": (
			"structural, not behavioural: the instant is read once in `ListRequest.__init__` and threaded, "
			"so a second read cannot be planted without rewriting the constructor — and a plant that "
			"returns a drifting clock changes no answer while the code is CORRECT, which would score a "
			"false negative against working code. The first attempt at this plant was a no-op returning "
			"exactly what the original returns, and the harness rightly reported it as undetected."
		),
	},
	{
		"id": "MUT-count-ignores-filter",
		"english": "the footer says '20 of 3075' while the screen shows a filtered eight (C7)",
		"target": "engine.ListRequest:_count",
		"make": _count_ignores_the_filter,
		"detector": PROMISE,
		"untestable_without_browser": None,
	},
	{
		"id": "MUT-calendar-window-off",
		"english": "the calendar asks for the whole table again whatever month is on screen",
		"target": "engine.ListRequest:_window",
		"make": _calendar_window_ignored,
		"detector": EDGES,
		"untestable_without_browser": None,
	},
	{
		"id": "MUT-retirement-blocked",
		"english": "a field whose source column changed can no longer be switched off",
		"target": "derived:verify",
		"make": _retirement_blocked,
		"detector": HEAD,
		"untestable_without_browser": "the disable path is refused by a proof that must genuinely fail; planting a verify() that "
		"always fails would also refuse every OTHER save in the module and score a false TP. Covered "
		"directly by TestRetiringAFieldIsNeverBlocked instead.",
	},
]


def testable():
	"""Mutations with a real plant. The ones scored into the confusion matrix.

	A plant is scored ONLY if it is a genuine defect that reaches a rep's data. Two kinds are excluded by
	declaration rather than deletion: a typo guard whose worst outcome is cosmetic, and a property that is
	structural rather than behavioural. Both stay in `MUTATIONS` with their reason, because a mutation set
	you can quietly shrink is a recall number you can quietly fake."""
	return [m for m in MUTATIONS if not m["untestable_without_browser"] and m["make"]]


def untestable():
	"""Mutations declared untestable in-process. Surfaced as warnings, never silently dropped."""
	return [m for m in MUTATIONS if m["untestable_without_browser"]]


def resolve(target):
	"""`"module:attribute"` -> `(owner_object, attribute_name)`, so a plant can be applied and undone."""
	path, attribute = target.split(":")
	owner = {"derived": derived, "engine": engine, "repair": repair}[path.split(".")[0]]
	for part in path.split(".")[1:]:
		owner = getattr(owner, part)
	return owner, attribute
