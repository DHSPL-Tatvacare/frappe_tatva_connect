// The Desk ticket form's pickers offer what the Helpdesk app offers: one call per ticket names each field's business-line filters; this script names no field.
frappe.ui.form.on("HD Ticket", {
	refresh(frm) {
		if (frm.is_new()) return;
		frappe.call({
			method: "tatva_connect.helpdesk.pickers.ticket_pickers",
			args: { name: frm.doc.name },
			callback(r) {
				const scoped = (r.message && r.message.filters) || {};
				Object.keys(scoped).forEach((fieldname) => frm.set_query(fieldname, () => ({ filters: scoped[fieldname] })));
			},
		});
	},
});
