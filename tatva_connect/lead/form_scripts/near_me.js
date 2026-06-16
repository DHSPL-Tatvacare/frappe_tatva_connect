// CRM Form Script (CRM Lead, Form view) — "Near Me" (LSQ-style). A header button next to the lead
// title opens a dialog with a Leaflet map (OSM tiles, no key) + a scrollable nearby-doctor list. The
// rep's GPS feeds the permission-scoped server query `tatva_connect.location.api.leads_near` (default
// 15 km), which returns only the rep's own leads with a clinic anchor in range.
//
// HEADER UI: the CRM Lead header has no `this.actions` slot, so — exactly like activity_log.js — this
// is a contained, best-effort capture-phase DOM injection: find the title row, insert one idempotent
// button (guarded by id), and a rAF-coalesced MutationObserver reapplies it across re-renders. If the
// markup ever changes the worst case is the button doesn't render; nothing else breaks.
//
// LEAFLET: loaded lazily from the bench-vendored assets (frappe ships Leaflet; the cluster plugin is
// vendored in tatva_connect). We inject the <link>/<script> tags once (guarded by id), then await a
// ready promise before building the map. No CDN, no API key — OSM raster tiles only.
//
// ROW ACTIONS: each doctor row carries two icon buttons —
//   • PHONE → mobile only: window.location='tel:'+mobile_no. On desktop (no coarse pointer) it is
//     disabled. Needs mobile_no, which leads_near now returns.
//   • DIRECTIONS → always opens Google Maps directions in a NEW TAB (works desktop + mobile).
// Clicking a row centres the map on that doctor.
//
// class CRMLead runs via the CRM native form-script lifecycle (onRender) with createDialog / call /
// toast injected. Helpers are self-contained (each form script is evaluated in its own scope).

const TCNM_BTN_ID = "tc-nearme-btn";
const TCNM_STYLE_ID = "tc-nearme-style";
const TCNM_RADIUS_KM = 15;
const TCNM_LEAFLET_CSS = "/assets/frappe/js/lib/leaflet/leaflet.css";
const TCNM_LEAFLET_JS = "/assets/frappe/js/lib/leaflet/leaflet.js";
const TCNM_CLUSTER_CSS = "/assets/tatva_connect/js/vendor/markercluster/MarkerCluster.css";
const TCNM_CLUSTER_CSS_DEFAULT = "/assets/tatva_connect/js/vendor/markercluster/MarkerCluster.Default.css";
const TCNM_CLUSTER_JS = "/assets/tatva_connect/js/vendor/markercluster/leaflet.markercluster.js";

const tcnmEsc = (s) =>
  String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
const tcnmMsg = (e) => (e && (e.messages?.[0] || e.message)) || "";

// Coarse pointer / touch ⇒ treat as mobile (dialer available). Desktop ⇒ phone button disabled.
function tcnmIsMobile() {
  try {
    if (window.matchMedia && window.matchMedia("(pointer: coarse)").matches) return true;
  } catch (e) {}
  return "ontouchstart" in window || (navigator.maxTouchPoints || 0) > 0;
}

function tcnmGetGPS() {
  return new Promise((resolve) => {
    if (!navigator.geolocation) return resolve(null);
    navigator.geolocation.getCurrentPosition(
      (p) => resolve({ lat: p.coords.latitude, lng: p.coords.longitude }),
      () => resolve(null),
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 }
    );
  });
}

function tcnmLoadCSS(href, id) {
  if (document.getElementById(id)) return;
  const l = document.createElement("link");
  l.id = id;
  l.rel = "stylesheet";
  l.href = href;
  document.head.appendChild(l);
}

function tcnmLoadScript(src, id) {
  return new Promise((resolve, reject) => {
    const existing = document.getElementById(id);
    if (existing) {
      if (existing.dataset.tcLoaded === "1") return resolve();
      existing.addEventListener("load", () => resolve());
      existing.addEventListener("error", () => reject(new Error("load failed: " + src)));
      return;
    }
    const s = document.createElement("script");
    s.id = id;
    s.src = src;
    s.async = true;
    s.addEventListener("load", () => {
      s.dataset.tcLoaded = "1";
      resolve();
    });
    s.addEventListener("error", () => reject(new Error("load failed: " + src)));
    document.head.appendChild(s);
  });
}

// Load Leaflet + the cluster plugin once; resolve when window.L (and L.markerClusterGroup) are ready.
// The cluster plugin extends L, so it must load AFTER leaflet.js.
async function tcnmEnsureLeaflet() {
  tcnmLoadCSS(TCNM_LEAFLET_CSS, "tc-leaflet-css");
  tcnmLoadCSS(TCNM_CLUSTER_CSS, "tc-cluster-css");
  tcnmLoadCSS(TCNM_CLUSTER_CSS_DEFAULT, "tc-cluster-css-default");
  await tcnmLoadScript(TCNM_LEAFLET_JS, "tc-leaflet-js");
  await tcnmLoadScript(TCNM_CLUSTER_JS, "tc-cluster-js");
  return window.L;
}

// SVG icons (frappe-ui icon names aren't reliably exposed to a raw form script DOM, so inline SVG —
// crisp, theme-tinted via currentColor, no dependency).
const TCNM_ICON_PHONE =
  '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 ' +
  '19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L8.09 ' +
  '9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.91.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92z"/></svg>';
const TCNM_ICON_DIR =
  '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round"><polygon points="3 11 22 2 13 21 11 13 3 11"/></svg>';

function tcnmDirectionsUrl(lat, lng) {
  return "https://www.google.com/maps/dir/?api=1&destination=" + encodeURIComponent(lat + "," + lng);
}

function tcnmDistanceLabel(m) {
  if (m == null) return "";
  return m < 1000 ? m + " m" : (m / 1000).toFixed(1) + " km";
}

class CRMLead {
  onRender() {
    this._tcnmLead = this.doc && this.doc.name;
    if (!this._tcnmLead) return;
    this._tcnmInjectStyle();
    this._tcnmSetupButton();
  }

  _tcnmInjectStyle() {
    if (document.getElementById(TCNM_STYLE_ID)) return;
    const st = document.createElement("style");
    st.id = TCNM_STYLE_ID;
    st.textContent =
      "#" + TCNM_BTN_ID + "{display:inline-flex;align-items:center;gap:6px;height:28px;padding:0 12px;" +
      "border-radius:8px;border:1px solid var(--outline-gray-2);background:var(--surface-white);" +
      "color:var(--ink-gray-8);font-size:13px;font-weight:500;cursor:pointer;white-space:nowrap}" +
      "#" + TCNM_BTN_ID + ":hover{background:var(--surface-gray-2)}" +
      "#" + TCNM_BTN_ID + " svg{display:block}" +
      ".tc-nm-wrap{display:flex;flex-direction:column;gap:10px}" +
      "@media (min-width:760px){.tc-nm-wrap{flex-direction:row}}" +
      ".tc-nm-map{width:100%;height:300px;border-radius:10px;overflow:hidden;background:var(--surface-gray-2);z-index:0}" +
      "@media (min-width:760px){.tc-nm-map{flex:1 1 55%;height:420px}}" +
      ".tc-nm-side{display:flex;flex-direction:column;min-height:0}" +
      "@media (min-width:760px){.tc-nm-side{flex:1 1 45%}}" +
      ".tc-nm-count{font-size:12px;color:var(--ink-gray-5);margin-bottom:6px}" +
      ".tc-nm-list{display:flex;flex-direction:column;gap:2px;max-height:40vh;overflow:auto}" +
      "@media (min-width:760px){.tc-nm-list{max-height:420px}}" +
      ".tc-nm-row{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:8px;cursor:pointer}" +
      ".tc-nm-row:hover{background:var(--surface-gray-2)}" +
      ".tc-nm-info{flex:1 1 auto;min-width:0}" +
      ".tc-nm-name{font-size:13px;font-weight:500;color:var(--ink-gray-8);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" +
      ".tc-nm-meta{font-size:12px;color:var(--ink-gray-5);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" +
      ".tc-nm-acts{display:flex;align-items:center;gap:4px;flex:none}" +
      ".tc-nm-ico{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;" +
      "border-radius:7px;border:1px solid var(--outline-gray-2);background:var(--surface-white);" +
      "color:var(--ink-gray-7);cursor:pointer;padding:0}" +
      ".tc-nm-ico:hover{background:var(--surface-gray-2);color:var(--ink-gray-9)}" +
      ".tc-nm-ico[disabled]{opacity:.4;cursor:not-allowed}" +
      ".tc-nm-empty{padding:24px;text-align:center;color:var(--ink-gray-5);font-size:13px}";
    document.head.appendChild(st);
  }

  // Find the lead header title row (the element holding the lead name) and insert the button next to
  // it. No stable hook in the CRM markup, so we anchor off the breadcrumb/title region by structure.
  _tcnmFindHeaderAnchor() {
    // The lead page header contains the title text; target the first heading-ish container in the page
    // header that isn't already ours. We use the breadcrumb container the CRM uses for actions.
    const headerBtnRow =
      document.querySelector(".sticky.top-0 .flex.items-center.gap-2") ||
      document.querySelector("header .flex.items-center.justify-between") ||
      document.querySelector("[class*='breadcrumb']");
    return headerBtnRow || null;
  }

  _tcnmInjectButton() {
    if (document.getElementById(TCNM_BTN_ID)) return; // idempotent
    const anchor = this._tcnmFindHeaderAnchor();
    if (!anchor) return;
    const ctl = this;
    const btn = document.createElement("button");
    btn.id = TCNM_BTN_ID;
    btn.type = "button";
    btn.innerHTML = TCNM_ICON_DIR + "<span>Near Me</span>";
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      ctl._tcnmOpen();
    });
    anchor.appendChild(btn);
  }

  // Header mounts/re-renders after onRender (tab switches, route changes), so a single inject can miss.
  // A rAF-coalesced MutationObserver (single handler parked on window, idempotent via the button id)
  // reapplies it, plus a few timed retries for the first mount.
  _tcnmSetupButton() {
    const ctl = this;
    let scheduled = false;
    const reapply = () => {
      if (scheduled) return;
      scheduled = true;
      requestAnimationFrame(() => {
        scheduled = false;
        ctl._tcnmInjectButton();
      });
    };
    if (window.__tcnmObserver) {
      try { window.__tcnmObserver.disconnect(); } catch (e) {}
    }
    const obs = new MutationObserver(reapply);
    window.__tcnmObserver = obs;
    obs.observe(document.body, { childList: true, subtree: true });
    this._tcnmInjectButton();
    [150, 500, 1200].forEach((t) => setTimeout(() => ctl._tcnmInjectButton(), t));
  }

  // ---- Near Me flow: GPS → leads_near → dialog with map + list -------------
  async _tcnmOpen() {
    const here = await tcnmGetGPS();
    if (!here) {
      this.toast.error("Allow location access to find nearby doctors.");
      return;
    }
    let leads;
    try {
      leads = (await this.call("tatva_connect.location.api.leads_near", {
        lat: here.lat, lng: here.lng, radius_km: TCNM_RADIUS_KM,
      })) || [];
    } catch (e) {
      this.toast.error(tcnmMsg(e) || "Couldn't load nearby doctors — please try again.");
      return;
    }

    const isMobile = tcnmIsMobile();
    const rows = leads.length
      ? leads
          .map((d, i) => {
            const phoneDisabled = !(isMobile && d.mobile_no);
            return (
              '<div class="tc-nm-row" data-idx="' + i + '">' +
              '<div class="tc-nm-info">' +
              '<div class="tc-nm-name">' + tcnmEsc(d.title) + "</div>" +
              '<div class="tc-nm-meta">' +
              (d.stage ? tcnmEsc(d.stage) + " · " : "") + tcnmEsc(tcnmDistanceLabel(d.distance_m)) +
              "</div></div>" +
              '<div class="tc-nm-acts">' +
              '<button class="tc-nm-ico tc-nm-phone" data-idx="' + i + '" ' +
              (phoneDisabled ? "disabled " : "") + 'title="Call">' + TCNM_ICON_PHONE + "</button>" +
              '<button class="tc-nm-ico tc-nm-dir" data-idx="' + i + '" title="Directions">' +
              TCNM_ICON_DIR + "</button>" +
              "</div></div>"
            );
          })
          .join("")
      : '<div class="tc-nm-empty">No doctors found within ' + TCNM_RADIUS_KM + " km of your location.</div>";

    this.createDialog({
      title: "Doctors near me",
      size: "3xl",
      html:
        '<div class="tc-nm-wrap">' +
        '<div class="tc-nm-map" id="tc-nm-map"></div>' +
        '<div class="tc-nm-side">' +
        '<div class="tc-nm-count">' + leads.length + " within " + TCNM_RADIUS_KM + " km</div>" +
        '<div class="tc-nm-list">' + rows + "</div>" +
        "</div></div>",
      actions: [{ label: "Close", onClick: (close) => close() }],
    });

    // Wire the map + row interactions once the dialog DOM is mounted.
    setTimeout(() => this._tcnmMountMap(here, leads, isMobile), 80);
  }

  async _tcnmMountMap(here, leads, isMobile) {
    const listEl = document.querySelector(".tc-nm-list");
    this._tcnmWireRows(listEl, leads, isMobile);

    const mapEl = document.getElementById("tc-nm-map");
    if (!mapEl) return;
    let L;
    try {
      L = await tcnmEnsureLeaflet();
    } catch (e) {
      mapEl.innerHTML =
        '<div class="tc-nm-empty">Map unavailable. Use the list below.</div>';
      return;
    }
    if (!L || !document.body.contains(mapEl)) return;

    const map = L.map(mapEl, { scrollWheelZoom: true }).setView([here.lat, here.lng], 12);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap contributors",
      maxZoom: 19,
    }).addTo(map);

    // "You" marker (a simple circle so we need no icon image assets).
    L.circleMarker([here.lat, here.lng], {
      radius: 8, color: "#2563eb", fillColor: "#2563eb", fillOpacity: 0.9, weight: 2,
    }).addTo(map).bindPopup("You");

    const markers = [];
    const group = L.markerClusterGroup ? L.markerClusterGroup() : null;
    const bounds = [[here.lat, here.lng]];
    leads.forEach((d) => {
      const m = L.marker([d.lat, d.lng]).bindPopup(
        "<b>" + tcnmEsc(d.title) + "</b>" +
        (d.stage ? "<br>" + tcnmEsc(d.stage) : "") +
        "<br>" + tcnmEsc(tcnmDistanceLabel(d.distance_m))
      );
      markers.push(m);
      bounds.push([d.lat, d.lng]);
      if (group) group.addLayer(m);
      else m.addTo(map);
    });
    if (group) map.addLayer(group);

    if (leads.length) {
      try { map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 }); } catch (e) {}
    }

    this._tcnmMap = map;
    this._tcnmMarkers = markers;
    // Leaflet mis-sizes inside a just-opened dialog; force a resize once laid out.
    setTimeout(() => { try { map.invalidateSize(); } catch (e) {} }, 120);
  }

  _tcnmWireRows(listEl, leads, isMobile) {
    if (!listEl) return;
    const ctl = this;
    listEl.querySelectorAll(".tc-nm-phone").forEach((b) => {
      b.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (b.hasAttribute("disabled")) return;
        const d = leads[+b.getAttribute("data-idx")];
        if (d && d.mobile_no && isMobile) window.location.href = "tel:" + d.mobile_no;
      });
    });
    listEl.querySelectorAll(".tc-nm-dir").forEach((b) => {
      b.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const d = leads[+b.getAttribute("data-idx")];
        if (d) window.open(tcnmDirectionsUrl(d.lat, d.lng), "_blank", "noopener");
      });
    });
    // Clicking a row (anywhere but the action buttons) centres the map on that doctor.
    listEl.querySelectorAll(".tc-nm-row").forEach((row) => {
      row.addEventListener("click", () => {
        const d = leads[+row.getAttribute("data-idx")];
        const m = ctl._tcnmMarkers && ctl._tcnmMarkers[+row.getAttribute("data-idx")];
        if (ctl._tcnmMap && d) {
          ctl._tcnmMap.setView([d.lat, d.lng], 15);
          if (m && m.openPopup) m.openPopup();
        }
      });
    });
  }
}
