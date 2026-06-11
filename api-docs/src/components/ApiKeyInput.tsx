import { useEffect, useState } from "react";

const ACTIVE_KEY = "tatva_partner_token"; // the one the playground injects
const LIST_KEY = "tatva_partner_tokens"; // all tokens this browser remembers
const TTL_MS = 30 * 60 * 1000; // tokens expire 30 min after they were saved/activated
const TEAL = "#0d9488"; // brand teal — visible in both light and dark
const LINE = "rgba(127,127,127,0.45)";

type Entry = { v: string; t: number }; // token value + last-saved timestamp (ms)

// Read + normalise the stored list, tolerating the old string[] format, and
// drop anything past its TTL.
const readFresh = (now: number): Entry[] => {
  try {
    const raw = localStorage.getItem(LIST_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(parsed)) return [];
    return parsed
      .map((e): Entry | null => {
        if (typeof e === "string") return { v: e, t: now };
        if (e && typeof e.v === "string" && typeof e.t === "number") return e;
        return null;
      })
      .filter((e): e is Entry => !!e && now - e.t < TTL_MS);
  } catch {
    return [];
  }
};

// Show a token as `token e1c3…810f0` — identifiable, not fully exposed.
const mask = (token: string): string => {
  const body = token.replace(/^token\s+/i, "");
  if (body.length <= 10) return token;
  return `token ${body.slice(0, 4)}…${body.slice(-4)}`;
};

const minsLeft = (savedAt: number, now: number): number =>
  Math.max(0, Math.ceil((TTL_MS - (now - savedAt)) / 60000));

// Lets you save MANY keys in this browser and flip the active one with a click.
// Each key self-expires after 30 min; the active one is mirrored to
// localStorage[ACTIVE_KEY], which the "Partner API key" playground identity injects.
export function ApiKeyInput() {
  const [val, setVal] = useState("");
  const [entries, setEntries] = useState<Entry[]>([]);
  const [active, setActive] = useState("");
  const [now, setNow] = useState(0);
  const [savedFlash, setSavedFlash] = useState(false);

  const sync = (list: Entry[], activeToken: string, ts: number) => {
    try {
      localStorage.setItem(LIST_KEY, JSON.stringify(list));
      if (activeToken) localStorage.setItem(ACTIVE_KEY, activeToken);
      else localStorage.removeItem(ACTIVE_KEY);
    } catch {
      /* ignore */
    }
    setEntries(list);
    setActive(activeToken);
    setNow(ts);
  };

  // Load on mount, migrate a pre-existing single token in, then re-prune every
  // 30s so expired keys drop out of the UI on their own.
  useEffect(() => {
    const prune = () => {
      const ts = Date.now();
      const list = readFresh(ts);
      let current = "";
      try {
        current = localStorage.getItem(ACTIVE_KEY) || "";
      } catch {
        /* ignore */
      }
      if (current && !list.some((e) => e.v === current)) {
        list.unshift({ v: current, t: ts });
      }
      if (current && !list.some((e) => e.v === current)) current = "";
      sync(list, current, ts);
    };
    prune();
    const id = setInterval(prune, 30000);
    return () => clearInterval(id);
  }, []);

  // Add (or renew) the pasted token and make it active.
  const save = () => {
    const token = val.trim();
    if (!token) return;
    const ts = Date.now();
    const list = [{ v: token, t: ts }, ...entries.filter((e) => e.v !== token)];
    sync(list, token, ts);
    setVal("");
    setSavedFlash(true);
    setTimeout(() => setSavedFlash(false), 1500);
  };

  // Activate an existing token and renew its 30-min window.
  const use = (token: string) => {
    const ts = Date.now();
    const list = entries.map((e) => (e.v === token ? { v: e.v, t: ts } : e));
    sync(list, token, ts);
  };

  const remove = (token: string) => {
    const ts = Date.now();
    const list = entries.filter((e) => e.v !== token);
    sync(list, token === active ? "" : active, ts);
  };

  const clearAll = () => sync([], "", Date.now());

  return (
    <div
      style={{
        border: `1px solid ${LINE}`,
        borderRadius: 10,
        padding: 16,
        margin: "16px 0",
      }}
    >
      <label
        htmlFor="tatva-api-key"
        style={{ display: "block", fontWeight: 600, marginBottom: 10 }}
      >
        Save your API key for the playground
      </label>
      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <input
          id="tatva-api-key"
          value={val}
          onChange={(e) => setVal(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && save()}
          placeholder="token <api_key>:<api_secret>"
          spellCheck={false}
          autoComplete="off"
          style={{
            flex: 1,
            minWidth: 0,
            boxSizing: "border-box",
            padding: "10px 12px",
            fontFamily: "monospace",
            fontSize: 13,
            color: "inherit",
            border: `1px solid ${LINE}`,
            borderRadius: 8,
            background: "rgba(127,127,127,0.06)",
            outline: "none",
          }}
        />
        <button
          type="button"
          onClick={save}
          style={{
            padding: "10px 18px",
            cursor: "pointer",
            borderRadius: 8,
            border: "none",
            background: TEAL,
            color: "#ffffff",
            fontWeight: 600,
            fontSize: 14,
            whiteSpace: "nowrap",
          }}
        >
          Save
        </button>
        {savedFlash && (
          <span style={{ color: TEAL, fontWeight: 600, fontSize: 14 }}>Saved ✓</span>
        )}
      </div>

      {entries.length > 0 && (
        <>
          <ul style={{ listStyle: "none", margin: "14px 0 0", padding: 0 }}>
            {entries.map((e) => {
              const isActive = e.v === active;
              return (
                <li
                  key={e.v}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    padding: "8px 10px",
                    borderRadius: 8,
                    background: isActive ? "rgba(13,148,136,0.10)" : "transparent",
                    border: `1px solid ${isActive ? TEAL : "transparent"}`,
                    marginTop: 6,
                  }}
                >
                  <span
                    aria-hidden
                    style={{ color: isActive ? TEAL : LINE, fontSize: 14, lineHeight: 1 }}
                  >
                    {isActive ? "●" : "○"}
                  </span>
                  <code style={{ flex: 1, minWidth: 0, fontSize: 13 }}>{mask(e.v)}</code>
                  <span style={{ fontSize: 12, opacity: 0.55, whiteSpace: "nowrap" }}>
                    expires in {minsLeft(e.t, now)}m
                  </span>
                  {isActive ? (
                    <span style={{ color: TEAL, fontWeight: 600, fontSize: 13 }}>Active</span>
                  ) : (
                    <button
                      type="button"
                      onClick={() => use(e.v)}
                      style={{
                        padding: "4px 12px",
                        cursor: "pointer",
                        borderRadius: 6,
                        border: `1px solid ${LINE}`,
                        background: "transparent",
                        color: "inherit",
                        fontSize: 13,
                      }}
                    >
                      Use
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => remove(e.v)}
                    aria-label="Remove token"
                    style={{
                      padding: "4px 10px",
                      cursor: "pointer",
                      borderRadius: 6,
                      border: "none",
                      background: "transparent",
                      color: "inherit",
                      opacity: 0.6,
                      fontSize: 16,
                      lineHeight: 1,
                    }}
                  >
                    ×
                  </button>
                </li>
              );
            })}
          </ul>
          <button
            type="button"
            onClick={clearAll}
            style={{
              marginTop: 12,
              padding: "6px 14px",
              cursor: "pointer",
              borderRadius: 8,
              border: `1px solid ${LINE}`,
              background: "transparent",
              color: "inherit",
              fontSize: 13,
              opacity: 0.85,
            }}
          >
            Clear all keys
          </button>
        </>
      )}

      <div style={{ fontSize: 12, opacity: 0.7, marginTop: 12 }}>
        Saved only in this browser, and each key <b>self-expires after 30 minutes</b>. Paste
        another key and <b>Save</b> to keep it too; <b>Use</b> switches the active one and
        renews its 30 minutes. The <b>Partner API key</b> identity in any playground injects
        whichever key is active.
      </div>
    </div>
  );
}

export default ApiKeyInput;
