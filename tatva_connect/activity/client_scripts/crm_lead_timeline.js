// Desk Client Script (CRM Lead, Form) — Activity Timeline + Location Captures sections.
//
// Renders two read-only sections from single server projections (one brain with the SPA feed):
//   • Activity Timeline  <- activity.api.lead_timeline   (custom_activity_timeline_html)
//   • Location Captures  <- location.api.lead_captures   (custom_location_captures_html):
//     each in-person capture as a row — static-map thumbnail (via the key-safe static_map proxy;
//     no Google key client-side) + date · rep · address, newest first.
// Native (Client Script + HTML field), no fork. Fail-safe: on error a section just stays empty.
frappe.ui.form.on("CRM Lead", {
  refresh(frm) {
    if (frm.is_new()) return;

    const esc = (s) =>
      String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    const muted = (msg) =>
      '<div style="color:var(--text-muted);font-size:12px;padding:6px 0">' + msg + "</div>";

    // ---- Activity Timeline -------------------------------------------------
    const tl = frm.get_field("custom_activity_timeline_html");
    if (tl) {
      frappe.call({
        method: "tatva_connect.activity.api.lead_timeline",
        args: { lead: frm.doc.name },
        callback: (r) => {
          const rows = (r && r.message) || [];
          if (!rows.length) {
            tl.$wrapper.html(muted("No activities logged yet."));
            return;
          }
          const line = (a) =>
            '<a href="/app/crm-task/' + encodeURIComponent(a.name) + '" ' +
            'style="display:flex;gap:8px;padding:7px 0;border-bottom:1px solid var(--border-color);' +
            'font-size:13px;color:var(--text-color);text-decoration:none">' +
            '<span style="color:var(--text-muted);white-space:nowrap">' + esc(a.date) + "</span>" +
            '<span style="color:var(--text-muted)">·</span>' +
            "<span>" + esc(a.owner_name) + "</span>" +
            '<span style="color:var(--text-muted)">·</span>' +
            '<span style="font-weight:600">' + esc(a.activity_type) + "</span>" +
            '<span style="color:var(--text-muted)">·</span>' +
            '<span style="color:var(--text-muted)">' + esc(a.status) + "</span>" +
            (a.address
              ? '<span style="color:var(--text-muted)">·</span>' +
                '<span style="color:var(--text-muted)">' + esc(a.address) + "</span>"
              : "") +
            "</a>";
          tl.$wrapper.html('<div style="display:flex;flex-direction:column">' + rows.map(line).join("") + "</div>");
        },
      });
    }

    // ---- Location Captures -------------------------------------------------
    const cap = frm.get_field("custom_location_captures_html");
    if (cap) {
      frappe.call({
        method: "tatva_connect.location.api.lead_captures",
        args: { lead: frm.doc.name },
        callback: (r) => {
          const rows = (r && r.message) || [];
          if (!rows.length) {
            cap.$wrapper.html(muted("No location captures yet."));
            return;
          }
          const thumb = (c) =>
            "/api/method/tatva_connect.location.api.static_map?lat=" +
            encodeURIComponent(c.lat) + "&lng=" + encodeURIComponent(c.lng);
          const row = (c) =>
            '<div style="display:flex;gap:10px;align-items:center;padding:8px 0;' +
            'border-bottom:1px solid var(--border-color)">' +
            '<img src="' + thumb(c) + '" alt="map" ' +
            'style="width:120px;height:70px;object-fit:cover;border-radius:6px;flex-shrink:0" ' +
            "onerror=\"this.style.display='none'\">" +
            '<div style="display:flex;flex-direction:column;gap:2px;min-width:0">' +
            '<div style="font-size:12px;color:var(--text-muted)">' + esc(c.date) + " · " + esc(c.rep) + "</div>" +
            '<div style="font-size:13px;color:var(--text-color)">' + esc(c.address || "—") + "</div>" +
            "</div></div>";
          cap.$wrapper.html('<div style="display:flex;flex-direction:column">' + rows.map(row).join("") + "</div>");
        },
      });
    }
  },
});
