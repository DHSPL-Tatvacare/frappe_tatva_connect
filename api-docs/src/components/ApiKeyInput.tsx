import { useEffect, useState } from "react";

const STORE_KEY = "tatva_partner_token";
const TEAL = "#0d9488"; // brand teal — visible in both light and dark
const LINE = "rgba(127,127,127,0.45)";

// Lets a partner paste their key ONCE; it's saved in this browser (localStorage)
// and the "Partner API key" playground identity injects it on every request.
export function ApiKeyInput() {
  const [val, setVal] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    try {
      setVal(localStorage.getItem(STORE_KEY) || "");
    } catch {
      /* SSR / no storage */
    }
  }, []);

  const save = () => {
    try {
      localStorage.setItem(STORE_KEY, val.trim());
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch {
      /* ignore */
    }
  };

  const clear = () => {
    try {
      localStorage.removeItem(STORE_KEY);
      setVal("");
    } catch {
      /* ignore */
    }
  };

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
      <input
        id="tatva-api-key"
        value={val}
        onChange={(e) => setVal(e.target.value)}
        placeholder="token <api_key>:<api_secret>"
        spellCheck={false}
        autoComplete="off"
        style={{
          display: "block",
          width: "100%",
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
      <div style={{ marginTop: 12, display: "flex", gap: 10, alignItems: "center" }}>
        <button
          type="button"
          onClick={save}
          style={{
            padding: "8px 18px",
            cursor: "pointer",
            borderRadius: 8,
            border: "none",
            background: TEAL,
            color: "#ffffff",
            fontWeight: 600,
            fontSize: 14,
          }}
        >
          Save
        </button>
        <button
          type="button"
          onClick={clear}
          style={{
            padding: "8px 18px",
            cursor: "pointer",
            borderRadius: 8,
            border: `1px solid ${LINE}`,
            background: "transparent",
            color: "inherit",
            fontSize: 14,
          }}
        >
          Clear
        </button>
        {saved && (
          <span style={{ color: TEAL, fontWeight: 600, fontSize: 14 }}>Saved ✓</span>
        )}
      </div>
      <div style={{ fontSize: 12, opacity: 0.7, marginTop: 10 }}>
        Saved only in this browser. Then pick the <b>Partner API key</b> identity in any
        operation's playground — it uses this automatically, so you never re-type it.
      </div>
    </div>
  );
}

export default ApiKeyInput;
