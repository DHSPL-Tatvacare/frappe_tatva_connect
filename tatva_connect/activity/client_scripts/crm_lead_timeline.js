// Desk Client Script (CRM Lead, Form) — the unified "Activity & Location" view.
//
// ONE read-only section, ONE projection (location.api.lead_location_view), ONE layout:
//   • Anchor card  — the doctor's source-of-truth location (Google static map + address + how it was set).
//   • Visit log    — every in-person visit, newest first: static-map thumbnail · type · status · rep ·
//                    time · distance from the anchor (✓ in range / ✗ out).
// Google static images only (key-safe static_map proxy — no key client-side). No second layout, no
// Leaflet, no comment-spam. Rendered into the single custom_activity_timeline_html field.
frappe.ui.form.on("CRM Lead", {
  refresh(frm) {
    if (frm.is_new()) return;
    // Search a place -> resolve lat/long -> set/move the geofence anchor (the right way to edit it).
    frm.add_custom_button(__("Set Clinic Location"), () => tcOpenClinicSearch(frm));
    const fld = frm.get_field("custom_activity_timeline_html");
    if (!fld) return;

    const esc = (s) =>
      String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    const muted = (m) => '<div style="color:var(--text-muted);font-size:12px;padding:8px 0">' + m + "</div>";
    const map1 = (lat, lng) =>
      "/api/method/tatva_connect.location.api.static_map?lat=" + encodeURIComponent(lat) + "&lng=" + encodeURIComponent(lng);

    frappe.call({
      method: "tatva_connect.location.api.lead_location_view",
      args: { lead: frm.doc.name },
      callback: (r) => {
        const data = (r && r.message) || {};
        const anchor = data.anchor;
        const visits = data.visits || [];
        if (!anchor && !visits.length) {
          fld.$wrapper.html(muted("No activity or location captures yet."));
          return;
        }

        let html = '<div style="display:flex;flex-direction:column;gap:14px">';

        // --- Anchor card ---
        if (anchor) {
          html +=
            '<div style="display:flex;gap:12px;align-items:center;border:1px solid var(--border-color);' +
            'border-radius:8px;padding:10px;background:var(--subtle-fg)">' +
            '<img src="' + map1(anchor.lat, anchor.lng) + '" alt="clinic" ' +
            'style="width:140px;height:84px;object-fit:cover;border-radius:6px;flex-shrink:0" ' +
            "onerror=\"this.style.display='none'\">" +
            '<div style="display:flex;flex-direction:column;gap:3px;min-width:0">' +
            '<div style="font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--text-muted)">Doctor location (anchor)</div>' +
            '<div style="font-size:13px;color:var(--text-color);font-weight:600">' + esc(anchor.address || "—") + "</div>" +
            '<div style="font-size:12px;color:var(--text-muted)">Source: ' + esc(anchor.source) + "</div>" +
            "</div></div>";
        }

        // --- Visit log ---
        if (visits.length) {
          html += '<div style="display:flex;flex-direction:column">';
          html +=
            '<div style="font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--text-muted);padding:2px 0 6px">' +
            "Visits (" + visits.length + ")</div>";
          for (const v of visits) {
            const inRange = v.distance_m == null ? "" :
              v.distance_m <= 0
                ? '<span style="color:var(--green-600)">at anchor</span>'
                : '<span style="color:var(--text-muted)">' + v.distance_m + " m away</span>";
            html +=
              '<a href="/app/crm-task/' + encodeURIComponent(v.task) + '" ' +
              'style="display:flex;gap:10px;align-items:center;padding:8px 0;border-top:1px solid var(--border-color);' +
              'text-decoration:none;color:var(--text-color)">' +
              '<img src="' + map1(v.lat, v.lng) + '" alt="map" ' +
              'style="width:96px;height:58px;object-fit:cover;border-radius:6px;flex-shrink:0" ' +
              "onerror=\"this.style.display='none'\">" +
              '<div style="display:flex;flex-direction:column;gap:2px;min-width:0">' +
              '<div style="font-size:13px;font-weight:600">' + esc(v.type) +
              (v.status ? ' <span style="font-weight:400;color:var(--text-muted)">· ' + esc(v.status) + "</span>" : "") + "</div>" +
              '<div style="font-size:12px;color:var(--text-muted)">' + esc(v.rep) + " · " + esc(v.date) +
              (inRange ? " · " + inRange : "") + "</div>" +
              (v.address ? '<div style="font-size:12px;color:var(--text-muted)">' + esc(v.address) + "</div>" : "") +
              "</div></a>";
          }
          html += "</div>";
        }

        html += "</div>";
        fld.$wrapper.html(html);
      },
    });
  },
});

// "Set Clinic Location" — Places API (New) type-ahead → resolve lat/long → move the geofence anchor.
// Server-proxied (key never client-side): place_autocomplete (suggestions) → place_details (coords) →
// set_clinic_location (writes the anchor, source = Doctor Address).
function tcOpenClinicSearch(frm) {
  let picked = null;
  let timer = null;
  const esc = frappe.utils.escape_html;
  const d = new frappe.ui.Dialog({
    title: __("Set Clinic Location"),
    fields: [
      { fieldtype: "Data", fieldname: "q", label: __("Search clinic / address"), reqd: 1 },
      { fieldtype: "HTML", fieldname: "out" },
    ],
    primary_action_label: __("Set as clinic"),
    primary_action() {
      if (!picked) {
        frappe.show_alert({ message: __("Search and pick a place first"), indicator: "orange" });
        return;
      }
      frappe.call({
        method: "tatva_connect.location.api.set_clinic_location",
        args: { lead: frm.doc.name, lat: picked.lat, lng: picked.lng, address: picked.address },
        freeze: true,
        callback: () => {
          d.hide();
          frappe.show_alert({ message: __("Clinic location set"), indicator: "green" });
          frm.reload_doc();
        },
      });
    },
  });
  const $out = () => d.fields_dict.out.$wrapper;
  const preview = () => {
    if (!picked) return "";
    const url = "/api/method/tatva_connect.location.api.static_map?lat=" +
      encodeURIComponent(picked.lat) + "&lng=" + encodeURIComponent(picked.lng);
    return '<div style="margin-top:8px"><div style="font-size:12px;color:var(--text-muted);margin-bottom:4px">📍 ' +
      esc(picked.address) + '</div><img src="' + url +
      '" style="width:100%;height:160px;object-fit:cover;border-radius:6px"></div>';
  };
  const search = (q) => {
    frappe.call({
      method: "tatva_connect.location.api.place_autocomplete",
      args: { query: q },
      callback: (r) => {
        const rows = r.message || [];
        if (!rows.length) {
          $out().html('<div style="padding:8px;color:var(--text-muted)">No matches.</div>');
          return;
        }
        const list = rows.map((m, i) =>
          '<div class="tc-pl-row" data-i="' + i + '" style="padding:8px;border-bottom:1px solid var(--border-color);cursor:pointer">' +
          esc(m.description) + "</div>").join("");
        $out().html('<div style="max-height:220px;overflow:auto;border:1px solid var(--border-color);border-radius:6px">' +
          list + '</div><div class="tc-pl-prev"></div>');
        $out().find(".tc-pl-row").on("click", function () {
          const m = rows[parseInt(this.getAttribute("data-i"))];
          frappe.call({
            method: "tatva_connect.location.api.place_details",
            args: { place_id: m.place_id },
            callback: (r2) => {
              if (!r2.message) {
                frappe.show_alert({ message: __("Couldn't resolve that place"), indicator: "red" });
                return;
              }
              picked = r2.message;
              $out().find(".tc-pl-prev").html(preview());
            },
          });
        });
      },
    });
  };
  d.fields_dict.q.$input.on("input", function () {
    const q = this.value.trim();
    picked = null;
    clearTimeout(timer);
    if (q.length < 3) { $out().html(""); return; }
    timer = setTimeout(() => search(q), 300);
  });
  d.show();
}
