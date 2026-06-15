// CRM Form Script (CRM Lead, Form view) — "Log Activity" entry point.
//
// Splits the Tasks-tab action into [New Task | Log Activity]: the native "New Task"
// button is left untouched (plain to-do), and a sibling "Log Activity" button is
// injected beside it (capture-resilient via a MutationObserver, same pattern as
// hide_status_pill.js / email_attach.js). Log Activity -> a searchable Activity Type
// picker (scoped to this lead's grain by list_types_for_lead) -> the chosen type's
// form rendered dynamically from get_schema -> save_activity (the one server writer).
//
// Native frappe-ui dialog ($dialog) + theme tokens, light/dark safe. No window globals
// for state, no hand-rolled overlay (the dialog IS native). DOM-based button injection;
// if a crm upgrade renames the markup the button just stops appearing (fail-safe).
function setupForm({ doc, $dialog, call, createToast }) {
  const WRAP_ID = "tc-activity-split";
  const notify = (m, ok) => createToast({ message: m, type: ok ? "success" : "error" });
  const esc = (s) =>
    String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  if (!document.getElementById("tc-activity-style")) {
    const st = document.createElement("style");
    st.id = "tc-activity-style";
    st.textContent =
      // a TRUE split button: [ + New Task | Log Activity ] — one joined, segmented control
      "#" + WRAP_ID + "{display:inline-flex;align-items:stretch;height:28px;border-radius:8px;" +
      "overflow:hidden;border:1px solid var(--outline-gray-2)}" +
      ".tc-split-seg{display:inline-flex;align-items:center;gap:6px;height:100%;padding:0 12px;border:none;" +
      "background:var(--surface-white);color:var(--ink-gray-8);font-size:13px;font-weight:500;" +
      "cursor:pointer;white-space:nowrap}" +
      ".tc-split-seg:hover{background:var(--surface-gray-2)}" +
      ".tc-split-alt{border-left:1px solid var(--outline-gray-2)}" +
      ".tc-split-plus{font-size:15px;line-height:1}" +
      ".tc-af-list{display:flex;flex-direction:column;gap:2px;max-height:50vh;overflow:auto}" +
      ".tc-af-row{display:flex;align-items:center;gap:8px;padding:9px 10px;border-radius:8px;cursor:pointer;" +
      "font-size:13px;color:var(--ink-gray-8)}.tc-af-row:hover{background:var(--surface-gray-2)}" +
      ".tc-af-empty{padding:18px;text-align:center;color:var(--ink-gray-5);font-size:13px}" +
      ".tc-af-field{display:flex;flex-direction:column;gap:5px;margin-bottom:12px}" +
      ".tc-af-field>label{font-size:12px;color:var(--ink-gray-6)}" +
      ".tc-af-field .tc-req{color:var(--ink-red-3)}" +
      ".tc-af-ctrl,.tc-af-search{width:100%;box-sizing:border-box;border:1px solid var(--outline-gray-2);" +
      "border-radius:8px;padding:7px 10px;font-size:13px;background:var(--surface-white);" +
      "color:var(--ink-gray-8);outline:none}.tc-af-ctrl:focus,.tc-af-search:focus{border-color:var(--outline-gray-3)}" +
      ".tc-af-search{margin-bottom:8px}textarea.tc-af-ctrl{min-height:64px;resize:vertical}";
    document.head.appendChild(st);
  }

  // ---- location capture (in-person + tracked activities, Phase B) ---------
  // Server owns the rule (location.api.location_guard_applies / set_or_check_anchor); the client
  // only reads device GPS (a browser-only API) and confirms via the native dialog. Reused by the
  // task-completion controller too — same shape, separate script context (no shared globals).
  function getFix() {
    return new Promise((resolve, reject) => {
      if (!navigator.geolocation) return reject(new Error("no geolocation"));
      navigator.geolocation.getCurrentPosition(
        (pos) => resolve({ lat: pos.coords.latitude, lng: pos.coords.longitude, accuracy: pos.coords.accuracy }),
        reject,
        { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 }
      );
    });
  }
  function confirmLocation(fix, address) {
    const mapUrl =
      "/api/method/tatva_connect.location.api.static_map?lat=" +
      encodeURIComponent(fix.lat) + "&lng=" + encodeURIComponent(fix.lng);
    const accTxt = fix.accuracy ? "Accuracy to " + fix.accuracy.toFixed(2) + " m" : "";
    const addrTxt = address || "Address unavailable";
    return new Promise((resolve) => {
      $dialog({
        title: "Location Fetched",
        html:
          '<img src="' + mapUrl + '" alt="map" style="width:100%;height:180px;object-fit:cover;' +
          "border-radius:8px;margin-bottom:12px\" onerror=\"this.style.display='none'\"/>" +
          '<div style="display:flex;gap:8px;align-items:flex-start"><span style="flex:0 0 auto;margin-top:1px">📍</span>' +
          '<div><div style="font-size:14px;line-height:1.5;color:var(--ink-gray-8)">' + esc(addrTxt) + "</div>" +
          '<div style="color:var(--ink-gray-5);font-size:12px;margin-top:4px">' + esc(accTxt) + "</div></div></div>",
        actions: [
          { label: "Confirm & Save", variant: "solid", onClick: (close) => { close(); resolve(true); } },
          { label: "Cancel", onClick: (close) => { close(); resolve(false); } },
        ],
      });
    });
  }
  // Capture+confirm a fix for an in-person tracked type; returns {lat,lng,accuracy} | null (abort).
  async function captureIfNeeded(type) {
    let needed = false;
    try {
      needed = await call("tatva_connect.location.api.location_needed", { lead: doc.name, task_type: type });
    } catch (e) {
      return {}; // probe failed -> let the server backstop decide; send no fix
    }
    if (!needed) return {};
    let fix;
    try {
      fix = await getFix();
    } catch (e) {
      notify("Location permission is required for this in-person activity.", false);
      return null;
    }
    let address = null;
    try {
      const r = await call("tatva_connect.location.api.reverse_geocode", { lat: fix.lat, lng: fix.lng });
      address = r && r.address;
    } catch (e) {}
    const okay = await confirmLocation(fix, address);
    if (!okay) return null;
    return { lat: fix.lat, lng: fix.lng, accuracy: fix.accuracy };
  }

  // ---- dynamic form (shared by every picked type) -------------------------
  const dt2local = (v) => (v && v.length === 16 ? v.replace("T", " ") + ":00" : v);

  function controlHtml(f) {
    const opts = (f.options || "").split("\n").map((o) => o.trim()).filter(Boolean);
    const a = 'class="tc-af-ctrl" data-fieldname="' + esc(f.fieldname) + '" data-fieldtype="' + esc(f.fieldtype) + '"';
    if (f.fieldtype === "Small Text") return "<textarea " + a + "></textarea>";
    if (f.fieldtype === "Select")
      return "<select " + a + '><option value=""></option>' +
        opts.map((o) => "<option>" + esc(o) + "</option>").join("") + "</select>";
    if (f.fieldtype === "Datetime") return '<input type="datetime-local" ' + a + " />";
    if (f.fieldtype === "Check") return '<input type="checkbox" ' + a + ' style="width:auto" />';
    return '<input type="text" ' + a + " />"; // Data, Link
  }

  function openForm(type, schema) {
    const fields = schema
      .map(
        (f) =>
          '<div class="tc-af-field"><label>' + esc(f.label) +
          (f.reqd ? ' <span class="tc-req">*</span>' : "") + "</label>" + controlHtml(f) + "</div>"
      )
      .join("");
    $dialog({
      title: type,
      html: '<div id="tc-act-form">' + (fields || '<div class="tc-af-empty">No fields — saving logs this activity.</div>') + "</div>",
      actions: [
        {
          label: "Save",
          variant: "solid",
          onClick: async (close) => {
            const values = {};
            for (const f of schema) {
              const el = document.querySelector('#tc-act-form [data-fieldname="' + CSS.escape(f.fieldname) + '"]');
              let v = !el ? "" : f.fieldtype === "Check" ? (el.checked ? 1 : 0) : el.value;
              if (f.fieldtype === "Datetime") v = dt2local(v);
              if (f.reqd && (v === "" || v == null)) {
                notify(f.label + " is required", false);
                return;
              }
              values[f.fieldname] = v;
            }
            const loc = await captureIfNeeded(type);
            if (loc === null) return; // capture denied / cancelled — keep the form open
            try {
              await call("tatva_connect.activity.api.save_activity", {
                lead: doc.name, task_type: type, values: JSON.stringify({ ...values, ...loc }),
              });
              close();
              notify("Activity logged · " + type, true);
            } catch (e) {
              notify((e && e.message) || "Could not save activity", false); // incl. out-of-range block
            }
          },
        },
      ],
    });
  }

  async function openForType(type) {
    let schema = [];
    try {
      schema = (await call("tatva_connect.activity.api.get_schema", { task_type: type })) || [];
    } catch (e) {
      notify("Could not load form", false);
      return;
    }
    openForm(type, schema);
  }

  // ---- type picker --------------------------------------------------------
  async function openPicker() {
    let types = [];
    try {
      types = (await call("tatva_connect.activity.api.list_types_for_lead", { lead: doc.name })) || [];
    } catch (e) {
      notify("Could not load activity types", false);
      return;
    }
    const rows = types.length
      ? types
          .map(
            (t) =>
              '<div class="tc-af-row" data-type="' + esc(t.name) + '" data-search="' +
              esc(t.label.toLowerCase()) + '">' + esc(t.label) + "</div>"
          )
          .join("")
      : '<div class="tc-af-empty">No activity types are configured for this lead.</div>';
    $dialog({
      title: "Log Activity",
      html:
        (types.length ? '<input class="tc-af-search" type="text" placeholder="Search activity types…">' : "") +
        '<div class="tc-af-list">' + rows + "</div>",
      actions: [{ label: "Close", onClick: (close) => close() }],
    });
    setTimeout(() => {
      const box = document.querySelector(".tc-af-search");
      if (box) {
        box.focus();
        box.addEventListener("input", () => {
          const q = box.value.trim().toLowerCase();
          document.querySelectorAll(".tc-af-row").forEach((el) => {
            el.style.display = !q || (el.getAttribute("data-search") || "").indexOf(q) !== -1 ? "" : "none";
          });
        });
      }
      document.querySelectorAll(".tc-af-row").forEach((el) => {
        el.addEventListener("click", () => {
          const type = el.getAttribute("data-type");
          // close the picker dialog, then open the form
          const closeBtn = [...document.querySelectorAll('[role="dialog"] button')].find(
            (b) => (b.textContent || "").trim() === "Close"
          );
          if (closeBtn) closeBtn.click();
          openForType(type);
        });
      });
    }, 60);
  }

  // ---- inject the SPLIT button: [ + New Task | Log Activity ] -------------
  // The native "New Task" button keeps its action: we hide it (don't destroy it) and the
  // "New Task" segment delegates to its click. "Log Activity" runs our flow. One joined control.
  function findNativeNewTask() {
    return [...document.querySelectorAll("button")].find(
      (b) => (b.textContent || "").trim() === "New Task" &&
        !b.closest("#" + WRAP_ID) && !b.querySelector("input")
    );
  }
  function clickNativeNewTask() {
    const n = document.querySelector("[data-tc-native-newtask]") || findNativeNewTask();
    if (n) n.click();
  }
  function makeSeg(label, cls, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tc-split-seg " + cls;
    b.innerHTML = label;
    b.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      onClick();
    });
    return b;
  }
  function injectSplit() {
    // keep the native button alive but hidden so the New Task segment can delegate to it
    const native = findNativeNewTask();
    if (native) {
      native.setAttribute("data-tc-native-newtask", "1");
      native.style.display = "none";
    }
    if (document.getElementById(WRAP_ID)) return; // control already placed
    const anchor = native || document.querySelector("[data-tc-native-newtask]");
    if (!anchor || !anchor.parentElement) return;
    const wrap = document.createElement("div");
    wrap.id = WRAP_ID;
    wrap.appendChild(makeSeg('<span class="tc-split-plus">+</span><span>New Task</span>', "tc-split-main", clickNativeNewTask));
    wrap.appendChild(makeSeg("Log Activity", "tc-split-alt", openPicker));
    anchor.parentElement.insertBefore(wrap, anchor);
  }

  let scheduled = false;
  function reapply() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      injectSplit();
    });
  }

  if (window.__tcActivityObserver) {
    try {
      window.__tcActivityObserver.disconnect();
    } catch (e) {}
  }
  const obs = new MutationObserver(reapply);
  window.__tcActivityObserver = obs;
  obs.observe(document.body, { childList: true, subtree: true });

  injectSplit();
  [150, 500, 1200].forEach((t) => setTimeout(injectSplit, t));
}
