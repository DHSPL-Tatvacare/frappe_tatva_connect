// Desk Client Script — CRM Intake Form builder (Frappe Desk, /app/crm-intake-form).
// Everything shown is decided server-side; this file paints and never judges.
//   1) State    -> ONE call to api.form_state: whether it is live, its public address,
//                  and why it cannot go live. `readiness` is the server's single answer (N3).
//   2) Grid     -> the mappings dropdowns come from the live brains, fed by tatva_set_grid_* .
//   3) Buttons  -> Publish/Unpublish beside Save; Live Form, Submissions and Web Form under View.
// Static teaching text is NOT here — it is `description` in the DocType JSON (N4).
// The grid mechanism lives in tatva_connect.bundle.js (tatva_set_grid_*), shared by every mapping surface.

frappe.ui.form.on('CRM Intake Form', {
  refresh(frm) {
    tatva_intake_state(frm);
    tatva_intake_showif_options(frm);
    tatva_intake_target_table_options(frm);
  },
  // A different record means a different set of places an answer can land, and a different Source list.
  target(frm) {
    tatva_intake_target_table_options(frm, true);
  },
});

frappe.ui.form.on('CRM Intake Field Map', {
  // A row was expanded — populate ITS target_field dropdown from the row's target_table.
  form_render(frm, cdt, cdn) {
    tatva_intake_target_field_options(frm, cdt, cdn);
  },
  // target_table changed on a row — refetch that row's target_field options.
  target_table(frm, cdt, cdn) {
    tatva_intake_target_field_options(frm, cdt, cdn);
  },
  // any source_field or fieldtype edit changes the show_if_field and depends_on_question option sets for every row.
  source_field(frm) {
    tatva_intake_showif_options(frm);
  },
  fieldtype(frm) {
    tatva_intake_showif_options(frm);
  },
  mappings_remove(frm) {
    tatva_intake_showif_options(frm);
  },
});

// ---- state: one round trip, painted three ways ------------------------------

function tatva_intake_state(frm) {
  frm.dashboard.clear_headline();
  frm.set_intro('');
  if (frm.is_new()) return;
  frappe.call({
    method: 'tatva_connect.intake.api.form_state',
    args: { intake_form: frm.doc.name },
    callback(r) {
      const state = (r && r.message) || {};
      // A save refreshes twice; clearing here, not before the call, keeps it to one headline.
      frm.dashboard.clear_headline();
      tatva_intake_headline(frm, state);
      tatva_intake_why_not_live(frm, state);
      tatva_intake_buttons(frm, state);
    },
  });
}

// What no field shows: whether the form is live, and the address to share — like every other Tatva banner, never a repeat of the fields below.
function tatva_intake_headline(frm, state) {
  const parts = ['<b>' + __('Status') + ':</b> ' + (state.published ? __('Live') : __('Not live'))];
  if (state.published && state.route) {
    // escape_html for what is shown, encodeURIComponent for what is followed.
    parts.push(
      '<b>' + __('Address') + ':</b> <a href="/' + encodeURIComponent(state.route) +
        '" target="_blank">/' + frappe.utils.escape_html(state.route) + '</a>'
    );
  }
  frm.dashboard.set_headline(parts.join(' &nbsp;·&nbsp; '));
}

// Why the form cannot go live, in the server's own words — `readiness`, verbatim (N3).
function tatva_intake_why_not_live(frm, state) {
  const reasons = state.reasons || [];
  if (!reasons.length) return;
  frm.set_intro(
    __('This form cannot go live yet:') +
      '<ul>' + reasons.map((x) => '<li>' + frappe.utils.escape_html(x) + '</li>').join('') + '</ul>',
    'orange'
  );
}

// ---- grid dropdowns ---------------------------------------------------------

// show_if_field: the OTHER rows' source_field values, column-wide (same for all rows).
function tatva_intake_showif_options(frm) {
  const grid = frm.fields_dict.mappings && frm.fields_dict.mappings.grid;
  if (!grid) return;
  const names = (frm.doc.mappings || [])
    .map((r) => (r.source_field || '').trim())
    .filter(Boolean);
  tatva_set_grid_column_options(grid, 'show_if_field', Array.from(new Set(names)));
  // Depends On: the other lookup questions, the only ones whose answer can narrow a list.
  const lookups = (frm.doc.mappings || []).filter((r) => r.fieldtype === 'Link').map((r) => (r.source_field || '').trim()).filter(Boolean);
  tatva_set_grid_column_options(grid, 'depends_on_question', Array.from(new Set(lookups)));
}

// target_table: where an answer may land, column-wide (same for all rows); Source follows the target's own list.
function tatva_intake_target_table_options(frm, target_changed) {
  const grid = frm.fields_dict.mappings && frm.fields_dict.mappings.grid;
  if (!grid) return;
  // A layer target lands answers on its own record and the records linked to it; the server names them, and none means a lead.
  frappe.call({
    method: 'tatva_connect.intake.api.target_profile',
    args: { target: frm.doc.target || '' },
    callback(r) {
      const profile = (r && r.message) || {};
      const destinations = profile.destinations || [];
      // Blank has always meant the default record; show it by name rather than as an empty box.
      if (!frm.doc.target) {
        frm.doc.target = profile.target;
        frm.refresh_field('target');
      }
      if (target_changed && frm.doc.source_doctype !== profile.source_doctype) {
        frm.set_value('source_doctype', profile.source_doctype);
        frm.set_value('source', '');
      } else if (!frm.doc.source_doctype) {
        // A form saved before Source followed its target: point the picker without dirtying the form; the next save stores it.
        frm.doc.source_doctype = profile.source_doctype;
      }
      frm.__intake_layer = destinations.length > 0;
      if (frm.__intake_layer) tatva_set_grid_column_options(grid, 'target_table', destinations);
      else tatva_intake_lead_sections(grid);
    },
  });
}

// A lead form's destinations: the live CRM Lead Section keys + note, read from the section brain.
function tatva_intake_lead_sections(grid) {
  frappe.db.get_list('CRM Lead Section', { fields: ['section_key'], order_by: 'display_order asc' }).then((rows) => {
    tatva_set_grid_column_options(grid, 'target_table', [...(rows || []).map((r) => r.section_key), 'note']);
  });
}

// target_field: per-row, from the ONE brain (CRM Lead API Field) scoped to the row's target_table AND
// the form's grain. The grain is read off frm.doc — the OPEN form, saved or not — so choosing/changing
// a grain re-narrows the list on the next dropdown open, with no save and no reject-after-the-fact.
function tatva_intake_target_field_options(frm, cdt, cdn) {
  const row = locals[cdt] && locals[cdt][cdn];
  if (!row || !(row.target_table || '').trim()) return;

  frappe.call({
    method: 'tatva_connect.intake.api.list_target_fields',
    args: {
      target_table: row.target_table,
      intake_form: frm.doc.name,
      vertical: frm.doc.custom_vertical,
      group: frm.doc.custom_group,
      program: frm.doc.custom_current_program,
      target: frm.doc.target,
    },
    callback(r) {
      const fields = (r && r.message) || [];
      // {value,label} pairs — frappe escapes these in the dropdown; we never build HTML.
      const data = fields.map((f) => ({ value: f.fieldname, label: f.label || f.fieldname }));
      const grid = frm.fields_dict.mappings && frm.fields_dict.mappings.grid;
      tatva_set_grid_row_options(grid, cdn, 'target_field', data);
      if (!frm.__intake_layer && !data.length && row.target_table !== 'note') {
        // Empty list has exactly one cause worth naming: no grain chosen yet.
        const g = [frm.doc.custom_vertical, frm.doc.custom_group, frm.doc.custom_current_program];
        if (!g.some((x) => (x || '').trim())) {
          frappe.show_alert({ message: __('Pick the Vertical / Group / Program first — the field list is grain-scoped.'), indicator: 'orange' });
        }
      }
    },
  });
}

// ---- buttons ----------------------------------------------------------------

// One action beside Save (take it live, or withdraw it); every link elsewhere sits under View.
function tatva_intake_buttons(frm, state) {
  const view = __('View');
  if (state.published && state.route) {
    frm.add_custom_button(__('Live Form'), () => window.open('/' + encodeURIComponent(state.route), '_blank'), view);
  }

  // Offered once scaffolded; when it is not ready the server refuses and names every reason.
  if (state.web_form) {
    frm.add_custom_button(state.published ? __('Unpublish') : __('Publish'), () => {
      frappe.call({
        method: 'tatva_connect.intake.api.toggle_published',
        args: { intake_form: frm.doc.name },
        freeze: true,
        freeze_message: state.published ? __('Withdrawing the form…') : __('Taking the form live…'),
        callback(r) {
          const live = !!(r && r.message);
          frappe.show_alert({
            message: live ? __('The form is live.') : __('The form has been withdrawn.'),
            indicator: live ? 'green' : 'orange',
          });
          frm.refresh();
        },
      });
    });

    // Opens the Web Form by NAME (autonamed off the title); a route is not a name and 404s here.
    frm.add_custom_button(__('Web Form'), () => frappe.set_route('Form', 'Web Form', state.web_form), view);
  }

  // This form's own submission table — built from the stamped doctype, nothing form-specific here.
  if (frm.doc.web_form_doctype) {
    frm.add_custom_button(__('Submissions'), () => frappe.set_route('List', frm.doc.web_form_doctype), view);
  }
}
