"""Data method for the 'Traffic by Channel' Dashboard Chart Source: one line per channel, per day, for the measure the chart's filters name.

Each line is frappe's own Count timeseries (`get_chart_config`) over that channel's native Number Card filters (`communications.traffic`).
"""
import frappe
from frappe.desk.doctype.dashboard_chart.dashboard_chart import get_chart_config
from frappe.utils import get_datetime, now_datetime

from tatva_connect.communications.traffic import lines, undated


@frappe.whitelist()
def get(chart_name=None, chart=None, no_cache=None, filters=None, from_date=None, to_date=None,
	time_interval=None, timespan=None, heatmap_year=None, **kwargs):
	measure = (frappe.parse_json(filters) or {}).get("measure")
	labels, datasets = [], []
	# The browser sends no dates; frappe's own `get` fills them the same way before calling `get_chart_config`.
	from_date = get_datetime(from_date) if from_date else None
	to_date = get_datetime(to_date) if to_date else now_datetime()
	for name, card in lines(measure).items():
		line = frappe._dict(document_type=card.document_type, based_on="creation", chart_type="Count", name=name,
			parent_document_type=card.parent_document_type)
		config = get_chart_config(line, undated(card), timespan or "Last Month", time_interval or "Daily", from_date, to_date)
		labels = config["labels"]
		datasets.append({"name": frappe._(name), "values": config["datasets"][0]["values"]})
	return {"labels": labels, "datasets": datasets}
