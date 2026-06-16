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
