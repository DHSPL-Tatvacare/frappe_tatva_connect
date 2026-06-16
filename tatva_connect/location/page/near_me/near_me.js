// Desk Page "Near Me" — doctor leads plotted by their clinic location, nearest first.
//
// Leaflet + OpenStreetMap tiles (no Google browser key, nothing leaks) + clustering (vendored
// Leaflet.markercluster). Data comes from one permission-scoped server query (location.api.leads_near
// — a rep sees only their own leads). Auto-locates the rep, shows the in-range list with distance,
// and clicking a row flies to that doctor. No grain hardcoded; works for every product's leads.
frappe.pages["near-me"].on_page_load = function (wrapper) {
  const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Near Me"), single_column: true });
  const esc = frappe.utils.escape_html;

  const $body = $(page.body).css({ padding: 0 });
  $body.html(
    '<div style="display:flex;flex-direction:column;height:calc(100vh - 130px)">' +
      '<div style="display:flex;gap:8px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--border-color)">' +
        '<span class="tc-nm-count" style="font-weight:600;color:var(--text-color)"></span>' +
        '<span style="flex:1"></span>' +
        '<select class="tc-nm-radius input-sm" style="width:auto;padding:4px 8px;border:1px solid var(--border-color);border-radius:6px;background:var(--control-bg)">' +
          '<option value="5">5 km</option><option value="10">10 km</option>' +
          '<option value="15" selected>15 km</option><option value="25">25 km</option><option value="50">50 km</option>' +
        "</select>" +
        '<button class="btn btn-default btn-sm tc-nm-locate">📍 ' + __("Locate me") + "</button>" +
      "</div>" +
      '<div class="tc-nm-map" style="flex:1;min-height:280px"></div>' +
      '<div class="tc-nm-list" style="height:36%;overflow:auto;border-top:1px solid var(--border-color)"></div>' +
    "</div>"
  );

  const $map = $body.find(".tc-nm-map")[0];
  const $list = $body.find(".tc-nm-list");
  const $count = $body.find(".tc-nm-count");
  let map, cluster, meMarker, center = null, byName = {};

  function initMap() {
    map = L.map($map).setView([12.9716, 77.5946], 11); // Bengaluru default until we locate
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19, attribution: "© OpenStreetMap",
    }).addTo(map);
    cluster = L.markerClusterGroup();
    map.addLayer(cluster);
  }

  function load() {
    if (!center) return;
    const radius = parseFloat($body.find(".tc-nm-radius").val());
    frappe.call({
      method: "tatva_connect.location.api.leads_near",
      args: { lat: center.lat, lng: center.lng, radius_km: radius },
      callback: (r) => render(r.message || []),
    });
  }

  function render(leads) {
    cluster.clearLayers();
    byName = {};
    const km = $body.find(".tc-nm-radius").val();
    $count.text(leads.length + " " + __("lead(s) within") + " " + km + " km");
    const rows = leads.map((d) => {
      const m = L.marker([d.lat, d.lng]).bindPopup(
        "<b>" + esc(d.title) + "</b><br>" + esc(d.stage) + "<br>" +
        (d.distance_m / 1000).toFixed(1) + " km<br>" +
        '<a href="/app/crm-lead/' + encodeURIComponent(d.name) + '">' + __("Open lead") + "</a>"
      );
      cluster.addLayer(m);
      byName[d.name] = { d: d, m: m };
      return (
        '<div class="tc-nm-row" data-name="' + esc(d.name) + '" ' +
        'style="display:flex;gap:10px;padding:10px 12px;border-bottom:1px solid var(--border-color);cursor:pointer">' +
        '<div style="flex:1;min-width:0">' +
          '<div style="font-weight:600;color:var(--text-color)">' + esc(d.title) + "</div>" +
          '<div style="font-size:12px;color:var(--text-muted)">' + esc(d.stage) + " · " +
            (d.distance_m / 1000).toFixed(1) + " km</div>" +
          '<div style="font-size:12px;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' +
            esc(d.address) + "</div>" +
        "</div></div>"
      );
    });
    $list.html(rows.join("") ||
      '<div style="padding:16px;color:var(--text-muted)">' + __("No doctors with a clinic location in range.") + "</div>");
    $list.find(".tc-nm-row").on("click", function () {
      const hit = byName[this.getAttribute("data-name")];
      if (hit) { map.setView([hit.d.lat, hit.d.lng], 16); hit.m.openPopup(); }
    });
  }

  function locate() {
    if (!navigator.geolocation) {
      frappe.show_alert({ message: __("Geolocation not available"), indicator: "red" });
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        center = { lat: pos.coords.latitude, lng: pos.coords.longitude };
        map.setView([center.lat, center.lng], 12);
        if (meMarker) map.removeLayer(meMarker);
        meMarker = L.circleMarker([center.lat, center.lng], {
          radius: 8, color: "#2490ef", fillColor: "#2490ef", fillOpacity: 0.9,
        }).addTo(map).bindPopup(__("You"));
        load();
      },
      () => frappe.show_alert({ message: __("Location permission denied"), indicator: "orange" }),
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 }
    );
  }

  frappe.require(
    [
      "/assets/frappe/js/lib/leaflet/leaflet.css",
      "/assets/frappe/js/lib/leaflet/leaflet.js",
      "/assets/tatva_connect/js/vendor/markercluster/MarkerCluster.css",
      "/assets/tatva_connect/js/vendor/markercluster/MarkerCluster.Default.css",
      "/assets/tatva_connect/js/vendor/markercluster/leaflet.markercluster.js",
    ],
    () => {
      initMap();
      $body.find(".tc-nm-locate").on("click", locate);
      $body.find(".tc-nm-radius").on("change", load);
      locate();
      setTimeout(() => map.invalidateSize(), 250);
    }
  );
};
