import frappe
from croniter import CroniterBadCronError, croniter
from frappe import _
from frappe.model.document import Document

from tatva_connect.tatva_analytics import engine, models


class TatvaAnalyticsControl(Document):
	def validate(self):
		model = models.by_job().get(self.name)
		if not model:
			frappe.throw(_("{0} has no model in tatva_analytics/models.").format(self.name))
		self.validate_window(model)
		self.validate_schedule(model)
		self.validate_parent()

	def validate_window(self, model):
		limit = engine.window_limit(model)
		if self.window_days and not 1 <= self.window_days <= limit:
			frappe.throw(_("Window (days) must be between 1 and {0}, or 0 for the job's default.").format(limit))
		before = self.get_doc_before_save()
		if before and before.watermark and engine.window_days(model, self) > engine.window_days(model, before):
			frappe.msgprint(_("The wider window fills only from the next changes. Press Rebuild to load the older days now."),
			                indicator="orange", alert=True)

	def validate_schedule(self, model):
		declared = getattr(model, "AFTER", None)
		if declared and (self.after_job != declared or self.frequency):
			frappe.throw(_("{0} reads the table {1} writes, so it always runs after {1} and takes no frequency.").format(self.name, declared))
		if self.frequency and self.after_job:
			frappe.throw(_("Pick a frequency or After job, not both."))
		if self.enabled and not (self.frequency or self.after_job):
			frappe.throw(_("Pick a frequency or After job before turning this job on."))
		if self.frequency != "Cron":
			self.cron_format = None
			return
		try:
			croniter(self.cron_format)
		except (CroniterBadCronError, TypeError):
			frappe.throw(_("{0} is not a valid cron expression.").format(self.cron_format))
		if not self.cron_format.split()[0].isdigit():
			frappe.throw(_("A job runs at most once an hour, so the cron minute must be one number, such as 0."))

	def validate_parent(self):
		chain, parent = [self.name], self.after_job
		while parent:
			if parent in chain:
				frappe.throw(_("After job makes a loop: {0}.").format(" → ".join([*chain, parent])))
			chain.append(parent)
			parent = frappe.db.get_value(engine.CONTROL, parent, "after_job")
		if self.enabled and self.after_job and not frappe.db.get_value(engine.CONTROL, self.after_job, "enabled"):
			frappe.throw(_("Turn on {0} first: this job runs after it.").format(self.after_job))
		if not self.enabled and self.has_value_changed("enabled"):
			after = frappe.get_all(engine.CONTROL, filters={"after_job": self.name, "enabled": 1}, pluck="name")
			if after:
				frappe.msgprint(_("{0} run after this job, so they stop too until it is back on.").format(", ".join(after)),
				                indicator="orange", alert=True)
