import { StrictMode, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import type { Fetcher } from "./api";
import { focusTarget } from "./labels";
import { Research, Simulation, type ViewProps } from "./ResearchViews";
import { httpSource, type ResearchSource, type SimulationSummary } from "./sources";
import { Settings, Strategies } from "./StrategyViews";
import { Accounts, Decisions, Overview, type Env } from "./views";

// Spec 13 primary navigation. Each screen is delivered by its own task.
const SECTIONS = [
  "Overview",
  "Accounts",
  "Decisions",
  "Watchlists",
  "Research",
  "Strategies",
  "Simulation",
  "Improvement",
  "Settings",
] as const;

type Section = (typeof SECTIONS)[number];
const VIEWS: Partial<Record<Section, (props: ViewProps) => ReactNode>> = {
  Overview, Accounts, Decisions, Research, Strategies, Simulation, Settings,
};

function sectionOf(hash: string): Section {
  return SECTIONS.find((name) => `#/${name.toLowerCase()}` === hash) ?? "Overview";
}

// Module constants so the environment, and with it every request, is stable across renders.
const browserFetch: Fetcher = (input, init) => fetch(input, init);
const clock = () => new Date();

export function App({
  fetcher = browserFetch,
  now = clock,
  research: injected,
}: { fetcher?: Fetcher; now?: () => Date; research?: ResearchSource }) {
  const env = useMemo<Env>(() => ({ fetcher, now }), [fetcher, now]);
  const research = useMemo(() => injected ?? httpSource(fetcher, now), [injected, fetcher, now]);
  // Simulation mode lives in the shell, so its label survives navigation between views.
  const [virtual, setVirtual] = useState<SimulationSummary | null>(null);
  const sim = useMemo(() => ({ current: virtual, set: setVirtual }), [virtual]);
  useEffect(() => {
    document.title = virtual ? "[SIMULATED] quantweave" : "quantweave";
  }, [virtual]);
  const [section, setSection] = useState(() => sectionOf(window.location.hash));
  const [moved, setMoved] = useState(0);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    const onHash = () => {
      if (!window.location.hash.startsWith("#/")) return; // in-page anchors are not routes
      setSection(sectionOf(window.location.hash));
      setMoved((n) => n + 1);
    };
    window.addEventListener("hashchange", onHash);
    return () => {
      window.removeEventListener("hashchange", onHash);
    };
  }, []);
  useEffect(() => {
    if (moved > 0) heading.current?.focus(); // after navigation, never on first load
  }, [moved]);
  const View = VIEWS[section];
  return (
    <>
      <a className="skip-link" href="#main" onClick={focusTarget}>Skip to main content</a>
      <header>
        <p>quantweave</p>
        <p role="note">Analysis only. This app never places, changes or cancels broker orders.</p>
        <p role="status" aria-label="Mode">
          {virtual ? (
            <>
              <strong>[SIMULATED]</strong> Simulation mode: {virtual.name}. Its figures are hypothetical, kept apart
              from real accounts and never added to them. Real-account screens still show real accounts only.{" "}
              <button type="button" onClick={() => { setVirtual(null); }}>Leave simulation mode</button>
            </>
          ) : "Mode: real-account analysis."}
        </p>
      </header>
      <nav aria-label="Primary">
        <ul>
          {SECTIONS.map((name) => (
            <li key={name}>
              <a href={`#/${name.toLowerCase()}`} aria-current={name === section ? "page" : undefined}>{name}</a>
            </li>
          ))}
        </ul>
      </nav>
      <main id="main" tabIndex={-1}>
        <h1 ref={heading} tabIndex={-1}>{section}</h1>
        {View ? <View env={env} research={research} sim={sim} /> : <p>This screen is not connected yet; it is delivered by its own task.</p>}
      </main>
    </>
  );
}

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
