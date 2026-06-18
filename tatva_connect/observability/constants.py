"""Single source of truth for the latency histogram.

The band edges are defined ONCE here and consumed everywhere:
- rollup.py        builds its `SUM(duration_ms ...)` band expressions from `histogram_sql()`
- the chart source + number cards read the same bands back via `percentile_from_counts()`
- crm_api_metric.json declares one column per band (kept in lockstep with LATENCY_BANDS;
  `assert_bands_match_schema()` guards against drift on migrate)

Bands are non-cumulative (disjoint): a request is counted in exactly one band, the one whose
`(prev_edge, edge]` interval contains its latency. The final band (`b_inf`, edge=None) is the
open-ended overflow bucket (> last edge). Percentiles are interpolated across the cumulative
band counts at read time — the Prometheus `histogram_quantile` approach.
"""

# Ordered (column, upper_edge_ms). edge=None => the open-ended overflow band (> previous edge).
LATENCY_BANDS = (
	("b10", 10),
	("b25", 25),
	("b50", 50),
	("b100", 100),
	("b250", 250),
	("b500", 500),
	("b1000", 1000),
	("b2500", 2500),
	("b5000", 5000),
	("b_inf", None),
)

# Just the column names, in order — used to build SELECT/SUM column lists.
HISTOGRAM_COLS = tuple(col for col, _ in LATENCY_BANDS)


def histogram_sql(field="duration_ms"):
	"""Ordered list of disjoint `SUM(...)` band expressions aligned to LATENCY_BANDS,
	for the raw -> 5min rollup. Generated from the bands so the edges are never retyped."""
	exprs, lower = [], None
	for _col, upper in LATENCY_BANDS:
		if upper is None:
			exprs.append(f"SUM({field} > {lower})")
		elif lower is None:
			exprs.append(f"SUM({field} <= {upper})")
		else:
			exprs.append(f"SUM({field} > {lower} AND {field} <= {upper})")
		if upper is not None:
			lower = upper
	return exprs


def percentile_from_counts(counts, total, p):
	"""Interpolate the p-th percentile (0-100) of latency from band counts.

	`counts` is a mapping of band column -> count (e.g. a metric row). Linear interpolation
	within the band that holds the target rank; the overflow band reports its lower edge
	(we can't see above it). Returns ms (float) or 0 when there's no traffic."""
	if not total:
		return 0
	target = (p / 100.0) * total
	cum, lower = 0, 0
	for col, upper in LATENCY_BANDS:
		c = counts.get(col) or 0
		if cum + c >= target:
			if upper is None:  # overflow band — nothing above to interpolate into
				return lower
			width = c or 1
			return round(lower + (target - cum) / width * (upper - lower), 1)
		cum += c
		if upper is not None:
			lower = upper
	return lower


def assert_bands_match_schema():
	"""Guard against LATENCY_BANDS drifting from the CRM API Metric columns. Called from
	schema_setup on after_migrate; raises if a band has no column or vice versa."""
	import frappe

	if not frappe.db.table_exists("CRM API Metric"):
		return
	cols = set(frappe.db.get_table_columns("CRM API Metric"))
	missing = [c for c in HISTOGRAM_COLS if c not in cols]
	if missing:
		frappe.throw(f"CRM API Metric is missing histogram columns {missing} declared in LATENCY_BANDS")
