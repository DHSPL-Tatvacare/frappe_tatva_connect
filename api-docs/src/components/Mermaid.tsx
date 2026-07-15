import { useEffect, useState } from "react";

// Renders a Mermaid diagram from its source. Zudoku does not wire the ```mermaid code fence to a
// renderer, so a diagram is placed with <Mermaid chart={`...`} /> in MDX instead. Rendering is
// client-only (mermaid touches the DOM), so the server emits nothing and the diagram appears on
// hydration; it re-renders when the light/dark theme flips.
export function Mermaid({ chart }: { chart: string }) {
  const [svg, setSvg] = useState("");

  useEffect(() => {
    let active = true;
    const render = async () => {
      const mermaid = (await import("mermaid")).default;
      const dark = document.documentElement.classList.contains("dark");
      mermaid.initialize({
        startOnLoad: false,
        theme: dark ? "dark" : "neutral",
        securityLevel: "loose",
        flowchart: { curve: "basis" },
      });
      const id = "mermaid-" + Math.abs(hash(chart)).toString(36);
      const { svg } = await mermaid.render(id, chart.trim());
      if (active) setSvg(svg);
    };
    render();

    // re-render when the theme class on <html> changes
    const observer = new MutationObserver(() => render());
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => {
      active = false;
      observer.disconnect();
    };
  }, [chart]);

  if (!svg) return <div className="my-6 h-4" aria-hidden />;
  return <div className="my-6 flex justify-center" dangerouslySetInnerHTML={{ __html: svg }} />;
}

// A small stable id from the chart text (mermaid needs a deterministic, unique element id).
function hash(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (Math.imul(31, h) + s.charCodeAt(i)) | 0;
  return h;
}
