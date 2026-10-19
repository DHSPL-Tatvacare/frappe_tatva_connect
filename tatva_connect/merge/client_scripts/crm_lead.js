// Desk Client Script — CRM Lead: Merge folds a duplicate lead into the one kept, within its grain.

frappe.ui.form.on('CRM Lead', { refresh: (frm) => tatva_merge_button(frm) });
