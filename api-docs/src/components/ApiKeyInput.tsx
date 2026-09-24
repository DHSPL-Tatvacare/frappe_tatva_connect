import { useEffect, useState } from "react";
import { Button } from "zudoku/ui/Button.js";
import { Input } from "zudoku/ui/Input.js";
import { cn } from "zudoku/ui/util.js";
import { ACTIVE_KEY, SAVED_KEYS, asAuthorization } from "../partner-key";

// A saved key leaves this browser 30 minutes after its last use; the key itself keeps working.
const TTL_MS = 30 * 60 * 1000;

type Entry = { v: string; t: number };

const load = (name: string) => {
  try {
    return localStorage.getItem(name);
  } catch {
    return null;
  }
};

const store = (name: string, value: string) => {
  try {
    if (value) localStorage.setItem(name, value);
    else localStorage.removeItem(name);
  } catch {
    return;
  }
};

const fresh = (now: number): Entry[] => {
  try {
    const list = JSON.parse(load(SAVED_KEYS) || "[]");
    if (!Array.isArray(list)) return [];
    return list.filter((e) => typeof e?.v === "string" && typeof e?.t === "number" && now - e.t < TTL_MS);
  } catch {
    return [];
  }
};

const mask = (key: string) => {
  const secret = asAuthorization(key).slice("token ".length);
  return secret.length <= 10 ? key : `token ${secret.slice(0, 4)}…${secret.slice(-4)}`;
};

const minsLeft = (savedAt: number, now: number) => Math.max(0, Math.ceil((TTL_MS - (now - savedAt)) / 60000));

export function ApiKeyInput() {
  const [val, setVal] = useState("");
  const [entries, setEntries] = useState<Entry[]>([]);
  const [active, setActive] = useState("");
  const [now, setNow] = useState(0);
  const [saved, setSaved] = useState(false);

  const sync = (list: Entry[], key: string, ts: number) => {
    store(SAVED_KEYS, JSON.stringify(list));
    store(ACTIVE_KEY, key);
    setEntries(list);
    setActive(key);
    setNow(ts);
  };

  useEffect(() => {
    const prune = () => {
      const ts = Date.now();
      const list = fresh(ts);
      const key = load(ACTIVE_KEY) || "";
      sync(list, list.some((e) => e.v === key) ? key : "", ts);
    };
    prune();
    const id = setInterval(prune, 30000);
    return () => clearInterval(id);
  }, []);

  const save = () => {
    const key = val.trim();
    if (!key) return;
    const ts = Date.now();
    sync([{ v: key, t: ts }, ...entries.filter((e) => e.v !== key)], key, ts);
    setVal("");
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  };

  const use = (key: string) => {
    const ts = Date.now();
    sync(entries.map((e) => (e.v === key ? { v: e.v, t: ts } : e)), key, ts);
  };

  const remove = (key: string) =>
    sync(entries.filter((e) => e.v !== key), key === active ? "" : active, Date.now());

  return (
    <div className="not-prose my-4 rounded-lg border p-4">
      <label htmlFor="tatva-api-key" className="mb-2.5 block font-semibold">
        Save an API key for the playground
      </label>
      <div className="flex items-center gap-2.5">
        <Input
          id="tatva-api-key"
          value={val}
          onChange={(e) => setVal(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && save()}
          placeholder="token <api_key>:<api_secret>"
          spellCheck={false}
          autoComplete="off"
          className="min-w-0 flex-1 font-mono"
        />
        <Button onClick={save}>Save</Button>
        {saved && <span className="text-sm font-semibold text-primary">Saved ✓</span>}
      </div>

      {entries.length > 0 && (
        <>
          <ul className="mt-3.5 space-y-1.5">
            {entries.map((e) => {
              const isActive = e.v === active;
              return (
                <li
                  key={e.v}
                  className={cn(
                    "flex items-center gap-2.5 rounded-md border px-2.5 py-2",
                    isActive ? "border-primary bg-primary/10" : "border-transparent",
                  )}
                >
                  <span aria-hidden className={cn("text-sm", isActive ? "text-primary" : "text-muted-foreground")}>
                    {isActive ? "●" : "○"}
                  </span>
                  <code className="min-w-0 flex-1 truncate text-xs">{mask(e.v)}</code>
                  <span className="whitespace-nowrap text-xs text-muted-foreground">cleared in {minsLeft(e.t, now)}m</span>
                  {isActive ? (
                    <span className="text-sm font-semibold text-primary">Active</span>
                  ) : (
                    <Button variant="outline" size="sm" onClick={() => use(e.v)}>
                      Use
                    </Button>
                  )}
                  <Button variant="ghost" size="icon-xs" aria-label="Remove key" onClick={() => remove(e.v)}>
                    ×
                  </Button>
                </li>
              );
            })}
          </ul>
          <Button variant="outline" size="sm" className="mt-3" onClick={() => sync([], "", Date.now())}>
            Clear all keys
          </Button>
        </>
      )}

      <p className="mt-3 text-xs text-muted-foreground">
        Your key is saved only in this browser. It's removed 30 minutes after you last use it, so it isn't left on a
        shared computer. The key itself keeps working. To keep more than one key, paste another and select{" "}
        <b>Save</b>. Select <b>Use</b> to switch keys.
      </p>
    </div>
  );
}

export default ApiKeyInput;
