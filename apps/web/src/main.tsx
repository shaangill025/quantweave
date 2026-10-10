import { StrictMode, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import type { Fetcher } from "./api";
import { focusTarget } from "./labels";
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
const VIEWS: Partial<Record<Section, (props: { env: Env }) => ReactNode>> = {
  Overview, Accounts, Decisions,
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
}: { fetcher?: Fetcher; now?: () => Date }) {
  const env = useMemo<Env>(() => ({ fetcher, now }), [fetcher, now]);
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
        <p role="note">Real-account analysis only. This app never places, changes or cancels broker orders.</p>
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
        {View ? <View env={env} /> : <p>This screen is not connected yet; it is delivered by its own task.</p>}
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
