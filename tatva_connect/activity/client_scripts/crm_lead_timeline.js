// Desk Client Script (CRM Lead, Form) — the unified "Activity & Location" view.
//
// ONE read-only section, ONE projection (location.api.lead_location_view), ONE layout:
//   • Anchor card  — the doctor's source-of-truth location (map + address + how it was set).
//   • Visit log    — every in-person visit, newest first: map thumbnail · type · status · rep ·
//                    time · distance from the anchor (✓ in range / ✗ out).
// Thumbnails follow the operator toggle (CRM Maps Settings → Thumbnail Map Provider, via map_config):
// Google static images (key-safe proxy) OR OpenStreetMap Leaflet maps rendered after insert — same
// switch as the SPA cards. Rendered into the single custom_activity_timeline_html field.
frappe.ui.form.on("CRM Lead", {
  refresh(frm) {
    if (frm.is_new()) return;
    // Search a place -> resolve lat/long -> set/move the geofence anchor (the right way to edit it).
    frm.add_custom_button(__("Set Clinic Location"), () => tcOpenClinicSearch(frm));
    // Near Me — the map of doctor leads around the rep (LSQ-style), opens centred on this clinic if set.
    frm.add_custom_button(__("Near Me"), () => {
      const lat = frm.doc.custom_clinic_latitude, lng = frm.doc.custom_clinic_longitude;
      frappe.set_route("near-me");
      if (lat && lng) frappe.route_options = { lat, lng };
    });
    const fld = frm.get_field("custom_activity_timeline_html");
    if (!fld) return;

    const esc = (s) =>
      String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    const muted = (m) => '<div style="color:var(--text-muted);font-size:12px;padding:8px 0">' + m + "</div>";
    const map1 = (lat, lng) =>
      "/api/method/tatva_connect.location.api.static_map?lat=" + encodeURIComponent(lat) + "&lng=" + encodeURIComponent(lng);

    // Resolve the thumbnail provider + zoom (operator toggle), then render the projection with it.
    frappe.call({ method: "tatva_connect.location.api.map_config" }).then((cfgR) => {
      const cfg = (cfgR && cfgR.message) || { thumbnail: "osm", zoom: 16 };
      const useOsm = cfg.thumbnail === "osm";
      const zoom = cfg.zoom || 16;
      const tileUrl = cfg.tile_url || "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
      // ONE thumbnail helper for every map in this view: a Google static image, or an OSM placeholder
      // (a div initialised into a Leaflet map after insert by tcInitOsmMaps). w/h in px; extra = styles.
      const thumb = (lat, lng, w, h, extra) => {
        extra = extra || "";
        if (useOsm) {
          return '<div class="tc-osm-map" data-lat="' + lat + '" data-lng="' + lng + '" ' +
            "style=\"width:" + w + "px;height:" + h + "px;border-radius:6px;flex-shrink:0;overflow:hidden;background:var(--subtle-fg);" + extra + "\"></div>";
        }
        return '<img src="' + map1(lat, lng) + '" alt="map" style="width:' + w + "px;height:" + h +
          "px;object-fit:cover;border-radius:6px;flex-shrink:0;" + extra + "\" onerror=\"this.style.display='none'\">";
      };

      frappe.call({
        method: "tatva_connect.location.api.lead_location_view",
        args: { lead: frm.doc.name },
        callback: (r) => {
          const data = (r && r.message) || {};
          const anchor = data.anchor;
          const acts = data.activities || [];
          const rejections = data.rejections || [];
          if (!anchor && !acts.length && !rejections.length) {
            fld.$wrapper.html(muted("No activities logged yet."));
            return;
          }

          let html = '<div style="display:flex;flex-direction:column;gap:14px">';

          // --- Anchor card ---
          if (anchor) {
            html +=
              '<div style="display:flex;gap:12px;align-items:center;border:1px solid var(--border-color);' +
              'border-radius:8px;padding:10px;background:var(--subtle-fg)">' +
              thumb(anchor.lat, anchor.lng, 140, 84) +
              '<div style="display:flex;flex-direction:column;gap:3px;min-width:0">' +
              '<div style="font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--text-muted)">Doctor location (anchor)</div>' +
              '<div style="font-size:13px;color:var(--text-color);font-weight:600">' + esc(anchor.address || "—") + "</div>" +
              '<div style="font-size:12px;color:var(--text-muted)">Source: ' + esc(anchor.source) + "</div>" +
              "</div></div>";
          }

          // --- Activity log (ALL completed activities; in-person rows carry a map + distance) ---
          if (acts.length) {
            html += '<div style="display:flex;flex-direction:column">';
            html +=
              '<div style="font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--text-muted);padding:2px 0 6px">' +
              "Activities (" + acts.length + ")</div>";
            for (const a of acts) {
              const meta =
                '<div style="display:flex;flex-direction:column;gap:2px;min-width:0">' +
                '<div style="font-size:13px;font-weight:600">' + esc(a.type) +
                (a.status ? ' <span style="font-weight:400;color:var(--text-muted)">· ' + esc(a.status) + "</span>" : "") + "</div>" +
                '<div style="font-size:12px;color:var(--text-muted)">' + esc(a.rep) + " · " + esc(a.date) +
                (a.located && a.distance_m != null
                  ? " · " + (a.distance_m <= 0
                      ? '<span style="color:var(--green-600)">at clinic</span>'
                      : '<span style="color:var(--text-muted)">' + a.distance_m + " m from clinic</span>")
                  : "") + "</div>" +
                (a.address ? '<div style="font-size:12px;color:var(--text-muted)">' + esc(a.address) + "</div>" : "") +
                "</div>";
              html +=
                '<a href="/app/crm-task/' + encodeURIComponent(a.task) + '" ' +
                'style="display:flex;gap:10px;align-items:center;padding:8px 0;border-top:1px solid var(--border-color);' +
                'text-decoration:none;color:var(--text-color)">' +
                (a.located
                  ? thumb(a.lat, a.lng, 96, 58)
                  : '<div style="width:96px;height:58px;border-radius:6px;flex-shrink:0;background:var(--subtle-fg);' +
                    'display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:11px">no visit</div>') +
                meta + "</a>";
            }
            html += "</div>";
          }

          // --- Blocked attempts (Rejected CRM Visit Audit rows — no task exists; the fraud/quality trail) ---
          if (rejections.length) {
            html += '<div style="display:flex;flex-direction:column">';
            html +=
              '<div style="font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--text-muted);padding:2px 0 6px">' +
              "Blocked attempts (" + rejections.length + ")</div>";
            for (const x of rejections) {
              const dist =
                x.distance_m != null
                  ? '<span style="color:var(--text-muted)">' + x.distance_m + " m from clinic" +
                    (x.allowed_m != null ? " · allowed " + x.allowed_m + " m" : "") + "</span>"
                  : "";
              const thumbHtml = x.lat && x.lng
                ? thumb(x.lat, x.lng, 96, 58, "opacity:.85;")
                : '<div style="width:96px;height:58px;border-radius:6px;flex-shrink:0;background:var(--subtle-fg);' +
                  'display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:11px">no fix</div>';
              html +=
                '<div style="display:flex;gap:10px;align-items:center;padding:8px 0;border-top:1px solid var(--border-color)">' +
                thumbHtml +
                '<div style="display:flex;flex-direction:column;gap:2px;min-width:0">' +
                '<div style="font-size:13px;font-weight:600">' + esc(x.rep) +
                ' <span style="font-weight:400;color:var(--red-600)">· Rejected</span></div>' +
                '<div style="font-size:12px;color:var(--text-muted)">' + esc(x.date) +
                (dist ? " · " + dist : "") + "</div>" +
                "</div></div>";
            }
            html += "</div>";
          }

          html += "</div>";
          fld.$wrapper.html(html);
          if (useOsm) tcInitOsmMaps(fld.$wrapper.get(0), zoom, tileUrl);
        },
      });
    });
  },
});

// Initialise every OSM placeholder (.tc-osm-map) into a small non-interactive Leaflet map. Loads
// Leaflet from Frappe's bundled assets on demand (global L); circleMarker avoids broken icon paths.
function tcInitOsmMaps(root, zoom, tileUrl) {
  const nodes = root.querySelectorAll(".tc-osm-map");
  if (!nodes.length) return;
  frappe.require(
    ["/assets/frappe/js/lib/leaflet/leaflet.css", "/assets/frappe/js/lib/leaflet/leaflet.js"],
    () => {
      nodes.forEach((node) => {
        if (node.dataset.tcInit) return;
        node.dataset.tcInit = "1";
        const lat = parseFloat(node.getAttribute("data-lat"));
        const lng = parseFloat(node.getAttribute("data-lng"));
        if (isNaN(lat) || isNaN(lng)) return;
        const map = L.map(node, {
          zoomControl: false, attributionControl: false, dragging: false, scrollWheelZoom: false,
          doubleClickZoom: false, boxZoom: false, keyboard: false, touchZoom: false,
        });
        L.tileLayer(tileUrl || "https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19 }).addTo(map);
        map.setView([lat, lng], zoom || 16);
        L.circleMarker([lat, lng], { radius: 6, color: "#ffffff", weight: 2, fillColor: "#2563eb", fillOpacity: 1 }).addTo(map);
        setTimeout(() => map.invalidateSize(), 80);
      });
    }
  );
}

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
