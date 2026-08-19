# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""The TatvaPractice field-visit review: the report a hundred people read every morning.

WHAT IT REPLACES. A spreadsheet exported from LSQ — one row per BDM, one column per visit type, the
month so far beside yesterday alone. This seeds the same question against CRM data, as an Insights
workbook, so the meeting reads a live surface instead of a file somebody prepared the night before.

WHY A FACT GRAIN AND NOT THE PIVOT. Insights appends a dashboard filter as an operation AFTER the
native SQL has run (ibis_utils.py:86) — a filter can never reach into the WHERE clause. So SQL that
pre-pivots by date answers one date for ever, and the date control on the page would silently filter
the already-aggregated rows. This query therefore emits the GRAIN — one row per date, BDM and visit
type — and the pivot, the totals and the two windows are all built above it by the charts. That is
also what lets one page carry both windows at once: a filter links to named charts only, so the date
control drives the day card and leaves the month card alone.

WHY `creation` IS THE VISIT DATE. `custom_completed_on` is the field that ought to hold it and it is
NULL on every TatvaPractice task — nothing in this codebase writes it. `creation` is not a proxy
standing in for it: on all but a handful of completed rows `creation` = `modified`, meaning the row is
BORN done at the moment the rep punches the activity. When reps start writing activities live those
two dates separate, and this is the line that has to change — see `_DATE_COLUMN`.

WHY THE WINDOW IS A CONSTANT. The scan cost IS the window, and it grows with it — the range seek reads
every completed task in the span and keeps only this vertical's. Month-to-date is what the meeting asks
for, so one month is what this reads. Widening it is one number, and it is not free.

HOW IT IS INDEXED. `ix_task_creation_status (creation, status)` is named explicitly. Left alone the
optimiser takes the plain `creation` index and fetches every row in the range to test its status; with
the composite the status is answered inside the index and the row is never fetched. FORCE INDEX and not
a hint, because the optimiser's own choice is stable, cheaper on paper and slower in fact.

WHAT THIS CANNOT SHOW. H.Q. The report has a column for it; nothing in this database holds it.
`tabUser.location`, `.city` and `.branch` are frappe's own and are empty for every enabled user, and
`CRM Sales Hierarchy` carries no location field at all. It is not a join that was missed — there is no
table to join to. When a roster lands, the column belongs on the hierarchy row beside `full_name` and
arrives here as one more SELECT.
"""

import json

import frappe

WORKBOOK_TITLE = "TatvaPractice Field Sales"

QUERY = "tp-field-visits"
QUERY_DAILY = "tp-visit-daily"
QUERY_MOMENTUM = "tp-visit-momentum"
CHART_DAY = "tp-visits-day"
CHART_MTD = "tp-visits-mtd"
CHART_TEAM = "tp-visits-team"
CHART_KPI_DAY = "tp-kpi-day"
CHART_KPI_MOM = "tp-kpi-mom"
CHART_TREND = "tp-trend"
CHART_TOP = "tp-top-bdms"
CHART_MIX = "tp-type-mix"
DASHBOARD = "tp-field-visit-review"

# The Insights team that reads it. It already exists and already holds this vertical's people — the page is added to what they can see, never the people to the team.
TEAM = "TatvaPractice"

DATA_SOURCE = "Site DB"

# The grain whose task types are field visits. A prefix and not a parsed key: `CRM Task Type` is named
# `{vertical}::{group}::{program}::{type_name}`, so this IS the grain, spelled the way the master spells it.
GRAIN_PREFIX = "Tatvapractice::India::Field-Sales::"

# Which column dates a visit. See the module docstring — this is the line that moves when reps go live.
_DATE_COLUMN = "creation"

# How far back the grain reaches. One calendar month = month-to-date, which is the question the meeting asks.
_WINDOW_MONTHS = 1

# How many days of history the trend line and the day-on-day cards read. The cost IS the window — every day added is more of the range to seek.
_TREND_DAYS = 45

# The index that answers `creation` range + `status` equality without fetching the row.
_INDEX = "ix_task_creation_status"

SQL = f"""
-- TatvaPractice field visits, one row per date / BDM / visit type.
-- Aggregate first, name second: the hierarchy and the type label are resolved against the GROUPED
-- rows, never the raw ones, which is the difference between three lookups and tens of thousands.
SELECT
    f.visit_date,
    f.is_month_to_date,
    tt.type_name                            AS visit_type,
    bdm.full_name                           AS bdm,
    COALESCE(asm.full_name, '(unmapped)')   AS reporting_manager,
    COALESCE(rsm.full_name, '(unmapped)')   AS regional_manager,
    f.visits
FROM (
    SELECT
        DATE(t.{_DATE_COLUMN})                                              AS visit_date,
        t.{_DATE_COLUMN} >= DATE_FORMAT(CURDATE(), '%Y-%m-01')              AS is_month_to_date,
        t.assigned_to                                                       AS bdm_user,
        t.custom_task_type                                                  AS type_key,
        COUNT(*)                                                            AS visits
    FROM `tabCRM Task` t FORCE INDEX ({_INDEX})
    WHERE t.{_DATE_COLUMN} >= DATE_SUB(DATE_FORMAT(CURDATE(), '%Y-%m-01'),
                                       INTERVAL {_WINDOW_MONTHS - 1} MONTH)
      AND t.{_DATE_COLUMN} <  CURDATE() + INTERVAL 1 DAY
      AND t.status = 'Done'
      AND t.custom_task_type LIKE '{GRAIN_PREFIX}%'
      AND t.assigned_to IS NOT NULL
    GROUP BY 1, 2, 3, 4
) f
JOIN      `tabCRM Task Type`       tt  ON tt.name  = f.type_key
JOIN      `tabCRM Sales Hierarchy` bdm ON bdm.user = f.bdm_user
LEFT JOIN `tabCRM Sales Hierarchy` asm ON asm.name = bdm.reports_to
LEFT JOIN `tabCRM Sales Hierarchy` rsm ON rsm.name = asm.reports_to
""".strip()


# The rows a field visit IS, spelled once so the three queries below cannot drift apart on what they count.
_VISIT_ROWS = f"""      AND t.status = 'Done'
      AND t.custom_task_type LIKE '{GRAIN_PREFIX}%'
      AND t.assigned_to IS NOT NULL"""

SQL_DAILY = f"""
-- One row per day, newest last. The Number card reads the LAST row as current and the one before it as
-- previous (NumberChart.vue:44), so the ORDER BY is not cosmetic — it is what makes the arrow mean
-- "against yesterday". It also feeds the sparkline and the trend line, which is why there is one series
-- and not three.
SELECT
    DATE(t.{_DATE_COLUMN})                                              AS visit_date,
    COUNT(*)                                                            AS visits,
    COUNT(DISTINCT t.assigned_to)                                       AS active_bdms,
    ROUND(COUNT(*) / NULLIF(COUNT(DISTINCT t.assigned_to), 0), 1)       AS visits_per_bdm
FROM `tabCRM Task` t FORCE INDEX ({_INDEX})
WHERE t.{_DATE_COLUMN} >= CURDATE() - INTERVAL {_TREND_DAYS} DAY
  AND t.{_DATE_COLUMN} <  CURDATE() + INTERVAL 1 DAY
{_VISIT_ROWS}
GROUP BY 1
ORDER BY 1
""".strip()

SQL_MOMENTUM = f"""
-- Two rows: the month so far, and the SAME span of the month before it. Never last month whole — at the
-- 14th that would read a full month against a half one and print a 50% collapse that never happened.
-- One indexed range covers both, and a two-row spine turns the single aggregate row into the two rows
-- the comparison card needs. Bounding the periods from a derived table instead loses the index entirely
-- and degrades to a full table scan; this shape keeps the seek.
SELECT
    s.seq,
    CASE s.seq WHEN 1 THEN a.prev_label  ELSE a.cur_label  END AS period,
    CASE s.seq WHEN 1 THEN a.prev_visits ELSE a.cur_visits END AS visits,
    CASE s.seq WHEN 1 THEN a.prev_bdms   ELSE a.cur_bdms   END AS active_bdms
FROM (SELECT 1 AS seq UNION ALL SELECT 2 AS seq) s
CROSS JOIN (
    SELECT
        DATE_FORMAT(CURDATE() - INTERVAL 1 MONTH, '%b %Y')                          AS prev_label,
        DATE_FORMAT(CURDATE(), '%b %Y')                                             AS cur_label,
        SUM(t.{_DATE_COLUMN} <  CURDATE() - INTERVAL 1 MONTH + INTERVAL 1 DAY)      AS prev_visits,
        SUM(t.{_DATE_COLUMN} >= DATE_FORMAT(CURDATE(), '%Y-%m-01'))                 AS cur_visits,
        COUNT(DISTINCT CASE WHEN t.{_DATE_COLUMN} < CURDATE() - INTERVAL 1 MONTH + INTERVAL 1 DAY
                            THEN t.assigned_to END)                                 AS prev_bdms,
        COUNT(DISTINCT CASE WHEN t.{_DATE_COLUMN} >= DATE_FORMAT(CURDATE(), '%Y-%m-01')
                            THEN t.assigned_to END)                                 AS cur_bdms
    FROM `tabCRM Task` t FORCE INDEX ({_INDEX})
    WHERE t.{_DATE_COLUMN} >= DATE_FORMAT(CURDATE() - INTERVAL 1 MONTH, '%Y-%m-01')
      AND t.{_DATE_COLUMN} <  CURDATE() + INTERVAL 1 DAY
{_VISIT_ROWS}
) a
ORDER BY s.seq
""".strip()


def _dimension(label, column):
	return {"dimension_name": label, "column_name": column, "data_type": "String"}


_VISITS = {"measure_name": "Visits", "column_name": "visits", "aggregation": "sum", "data_type": "Integer"}

# Only rows in the current calendar month. The day cards carry no filter of their own — the page's date control narrows them, and it links to those two charts alone.
_MTD_ONLY = {
	"logical_operator": "And",
	"filters": [{"column": {"type": "column", "column_name": "is_month_to_date"}, "operator": "=", "value": 1}],
}


def _table(rows, columns, values, filters=None, totals=True):
	config = {
		"rows": rows,
		"columns": columns,
		"values": values,
		"show_column_totals": True,
		"show_row_totals": totals,
		"order_by": [],
	}
	if filters:
		config["filters"] = filters
	return config


def _numbers(measures, *, date_column=None, comparison=False, sparkline=False, per_column=None):
	"""A KPI card holding SEVERAL measures. `comparison` is the arrow, and it is not a period calculation —
	the card takes the last row of the result as now and the row before it as then (NumberChart.vue:44),
	so it means something only on a query returning one row per period in order. Both cards read such a
	query. `per_column` carries per-measure overrides by position, falling back to the card's own."""
	config = {
		"number_columns": list(measures),
		"number_column_options": list(per_column or [{} for _ in measures]),
		"comparison": comparison,
		"negative_is_better": False,
		"sparkline": sparkline,
		"sparkline_color": "#318AD8",
		"shorten_numbers": False,
		"decimal": 0,
		"order_by": [],
	}
	if date_column:
		config["date_column"] = {"dimension_name": "Date", "column_name": date_column, "data_type": "Date"}
	return config


def _axis(dimension, measure, kind, *, granularity=None, area=False):
	"""A bar or a line. Both carry the same x/y shape; only the series type differs."""
	dim = dict(dimension)
	if granularity:
		dim["granularity"] = granularity
		dim["data_type"] = "Date"
	series = {"measure": measure, "type": kind}
	if kind == "line":
		series.update({"show_area": area, "smooth": True})
	y = {"series": [series]}
	if kind == "bar":
		y["stack"] = False
	return {"x_axis": {"dimension": dim}, "y_axis": y, "order_by": []}


def _donut(dimension, measure, slices=8):
	return {"label_column": dimension, "value_column": measure, "legend_position": "right",
	        "max_slices": slices, "order_by": []}


# The BDM identity, in the order the meeting reads it — the person, then who answers for them.
_WHO = [_dimension("BDM", "bdm"), _dimension("Reporting Manager", "reporting_manager"), _dimension("Regional Manager", "regional_manager")]

_MEASURE_VISITS = {"measure_name": "Visits", "column_name": "visits", "aggregation": "sum", "data_type": "Integer"}


def _measure(label, column):
	return {"measure_name": label, "column_name": column, "aggregation": "sum", "data_type": "Integer"}


# name, title, chart type, config, and WHICH query answers it. Three queries feed this page and a chart
# must say which, because the two series queries carry no BDM or manager column at all.
_CHARTS = (
	# ONE card per query, holding every measure that query answers — not one chart per number. The card
	# grid is a CONTAINER query (NumberChart.vue:95): it draws 2 columns under 576px, 3 under 768, 5 above
	# 896. A single measure in a wide box therefore renders at a fraction of its width, which is what left
	# the first cut looking broken. Measures and columns are matched here instead: three in a 8-wide box
	# and two in a 6-wide box each fill their grid exactly.
	(CHART_KPI_DAY, "Yesterday", "Number", _numbers(
		[_measure("Visits", "visits"), _measure("Reps in the field", "active_bdms"), _measure("Visits per rep", "visits_per_bdm")],
		date_column="visit_date", comparison=True, sparkline=True,
		per_column=[{}, {}, {"decimal": 1}],
	), QUERY_DAILY),
	(CHART_KPI_MOM, "Month to date", "Number", _numbers(
		[_measure("Visits", "visits"), _measure("Reps in the field", "active_bdms")],
		date_column="period", comparison=True,
	), QUERY_MOMENTUM),

	# --- the shape of the last six weeks, and who is carrying it.
	(CHART_TREND, "Visits per day", "Line", _axis({"dimension_name": "Date", "column_name": "visit_date", "data_type": "Date"}, _MEASURE_VISITS, "line", granularity="day", area=True), QUERY_DAILY),
	(CHART_TOP, "Busiest reps, month to date", "Bar", _axis({"dimension_name": "BDM", "column_name": "bdm", "data_type": "String"}, _MEASURE_VISITS, "bar"), QUERY),
	(CHART_MIX, "What the visits were", "Donut", _donut({"dimension_name": "Visit Type", "column_name": "visit_type", "data_type": "String"}, _MEASURE_VISITS), QUERY),

	# --- the roll-up a review is run from, then the two detail tables, stacked.
	(CHART_TEAM, "Month to date, by team", "Table", _table([_dimension("Regional Manager", "regional_manager"), _dimension("Reporting Manager", "reporting_manager")], [_dimension("Visit Type", "visit_type")], [_VISITS], _MTD_ONLY), QUERY),
	(CHART_DAY, "Visits on the day, by BDM", "Table", _table(_WHO, [_dimension("Visit Type", "visit_type")], [_VISITS]), QUERY),
	(CHART_MTD, "Month to date, by BDM", "Table", _table(_WHO, [_dimension("Visit Type", "visit_type")], [_VISITS], _MTD_ONLY), QUERY),
)


def _heading(title, standfirst):
	"""A band header. The item takes raw HTML (DashboardText.vue renders it with v-html), so the page can
	carry the sentence that says what the band below is for — which is the difference between a wall of
	charts and a report somebody can chair a meeting from."""
	return (f'<p style="margin:0;font-size:15px;font-weight:600;color:#1F272E">{title}</p>'
	        f'<p style="margin:2px 0 0;font-size:12px;color:#6B7580">{standfirst}</p>')


# x, y, w, h on the 20-column grid (dashboard.ts:99). EVERY band totals 20 so nothing can float: the
# first cut left columns 12-20 empty beside the filters and the grid's vertical compaction pulled the
# month card up into the hole. Read top to bottom — what happened, how it is tracking, where it came
# from, then rep by rep.
_BANDS = (
	("heading", _heading("Yesterday in the field", "The last day the data holds. Arrows compare it with the day before."), (0, 2, 20, 1)),
	(CHART_KPI_DAY, None, (0, 3, 8, 3)),
	(CHART_KPI_MOM, None, (8, 3, 6, 3)),
	("note", '<p style="margin:0;font-size:12px;line-height:1.5;color:#6B7580">Month to date is measured against the <b>same span</b> of the previous month, never the whole of it &mdash; so a comparison made on the 14th reads fourteen days against fourteen.</p>', (14, 3, 6, 3)),

	("heading2", _heading("How the month is tracking", "Six weeks of daily volume, and the reps carrying it."), (0, 6, 20, 1)),
	(CHART_TREND, None, (0, 7, 12, 7)),
	(CHART_TOP, None, (12, 7, 8, 7)),

	("heading3", _heading("Where the visits came from", "The mix of activity, and the same month split by the manager who answers for it."), (0, 14, 20, 1)),
	(CHART_MIX, None, (0, 15, 7, 8)),
	(CHART_TEAM, None, (7, 15, 13, 8)),

	("heading4", _heading("Rep by rep", "The review itself. Every BDM, every visit type, with a row total."), (0, 23, 20, 1)),
	(CHART_DAY, None, (0, 24, 20, 10)),
	(CHART_MTD, None, (0, 34, 20, 10)),
)

# Which charts a filter can honestly narrow: the ones fed by the fact grain, because only it carries a
# BDM and a manager. The two series queries are deliberately org-wide — a Number card compares the last
# row against the one before it, so a filter that cut the series to a single row would leave nothing to
# compare and the arrow would read against blank.
_FILTERABLE = (CHART_TOP, CHART_MIX, CHART_TEAM, CHART_DAY, CHART_MTD)

# The date reaches the DAY table alone: the month cards answer about the month by definition, and the
# strip along the top always reports the newest day the data holds. Three filters fill the row exactly.
_FILTERS = (
	("Visit Date", "Date", "visit_date", (CHART_DAY,), (0, 0, 6, 2)),
	("Regional Manager", "String", "regional_manager", _FILTERABLE, (6, 0, 7, 2)),
	("Reporting Manager", "String", "reporting_manager", _FILTERABLE, (13, 0, 7, 2)),
)


def _workbook():
	"""The workbook these live in, found by title. Its name is an autoincrement integer, so the title is
	the only stable handle — and one already exists on prod, holding the lead and activity queries."""
	existing = frappe.db.get_value("Insights Workbook", {"title": WORKBOOK_TITLE}, "name")
	if existing:
		return existing
	book = frappe.get_doc({"doctype": "Insights Workbook", "title": WORKBOOK_TITLE}).insert(ignore_permissions=True)
	return book.name


def _upsert(doctype, name, structural):
	"""Assert what this app's code depends on, and leave the rest to whoever edited it.

	The same split the dashboard seed makes: a query's SQL and a chart's dimensions are ours and are
	re-asserted every migrate, so a card re-pointed at the wrong column heals; a TITLE is the operator's
	and is written only when the row is born, so a heading reworded in Insights survives the next deploy.
	"""
	if not frappe.db.exists(doctype, name):
		frappe.get_doc({"doctype": doctype, **structural}).insert(ignore_permissions=True, set_name=name)
		return
	doc = frappe.get_doc(doctype, name)
	changed = [key for key, value in structural.items() if key != "title" and doc.get(key) != value]
	if not changed:
		return
	for key in changed:
		doc.set(key, structural[key])
	doc.save(ignore_permissions=True)


def _disown_data_query(chart):
	"""Clear a chart's `data_query` where it points at the SOURCE query, so `before_save` mints a fresh one.

	`data_query` is the chart's OWN derived query — `set_data_query` (insights_chart_v3.py:68) inserts an
	empty one per chart and the seed must never name it. Pointed at the source, the chart is its own input
	and the builder stops with "Circular query reference detected". Setting it back to None is the repair
	AND the guard: the field is only ever filled by the framework, so a chart seeded before this fix heals
	on the next migrate rather than needing a patch.
	"""
	if frappe.db.get_value("Insights Chart v3", chart, "data_query") == QUERY:
		frappe.db.set_value("Insights Chart v3", chart, "data_query", None)


def ensure_rows():
	"""Seed the query, its charts and the page. Idempotent, and safe to run on every migrate."""
	if not frappe.db.exists("Insights Data Source v3", DATA_SOURCE):
		return
	workbook = _workbook()

	for name, title, sql in (
		(QUERY, "TatvaPractice Field Visits", SQL),
		(QUERY_DAILY, "TatvaPractice Visits per Day", SQL_DAILY),
		(QUERY_MOMENTUM, "TatvaPractice Month on Month", SQL_MOMENTUM),
	):
		_upsert("Insights Query v3", name, {
			"title": title,
			"workbook": workbook,
			"is_native_query": 1,
			"use_live_connection": 1,  # the warehouse is a periodic copy; a day -1 review has to read the site DB
			"operations": json.dumps([{"type": "sql", "data_source": DATA_SOURCE, "raw_sql": sql}]),
		})

	for name, title, chart_type, config, source in _CHARTS:
		_disown_data_query(name)
		_upsert("Insights Chart v3", name, {
			"title": title,
			"workbook": workbook,
			"query": source,
			"chart_type": chart_type,
			"config": json.dumps(config),
		})

	_upsert("Insights Dashboard v3", DASHBOARD, {
		"title": "TatvaPractice Field Visit Review",
		"workbook": workbook,
		"items": json.dumps(_items()),
	})

	_share()


def _items():
	"""The page: its filters, then its cards, each with the grid box it sits in.

	A filter names the charts it narrows (`links`) rather than the page, which is what lets one date
	control drive the day cards while the month cards keep answering about the month."""
	source_of = {name: source for name, _t, _k, _c, source in _CHARTS}
	items = []
	for label, kind, column, links, (x, y, w, h) in _FILTERS:
		items.append({
			"type": "filter",
			"filter_name": label,
			"filter_type": kind,
			"links": {chart: f"`{source_of[chart]}`.`{column}`" for chart in links},
			"layout": {"i": f"filter-{label}", "x": x, "y": y, "w": w, "h": h},
		})
	for key, text, (x, y, w, h) in _BANDS:
		layout = {"i": key, "x": x, "y": y, "w": w, "h": h}
		items.append({"type": "text", "text": text, "layout": layout} if text is not None
		             else {"type": "chart", "chart": key, "layout": layout})
	return items


def _share():
	"""Let the TatvaPractice team see the page, the way the other two verticals already do.

	Adds the DASHBOARD to a team that exists and whose roster is somebody else's decision — this grants
	an existing group one more resource, and never creates a team or adds a member. Each vertical's team
	already carries exactly this pair of rows (its dashboard and the data source), so this is the shape
	the site is already in, asserted rather than left to be remembered after a deploy.
	"""
	if not frappe.db.exists("Insights Team", TEAM):
		return
	team = frappe.get_doc("Insights Team", TEAM)
	held = {(row.resource_type, row.resource_name) for row in team.team_permissions}
	wanted = [("Insights Dashboard v3", DASHBOARD), ("Insights Data Source v3", DATA_SOURCE)]
	missing = [pair for pair in wanted if pair not in held]
	if not missing:
		return
	for resource_type, resource_name in missing:
		team.append("team_permissions", {"resource_type": resource_type, "resource_name": resource_name})
	team.save(ignore_permissions=True)
