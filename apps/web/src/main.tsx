import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

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

export function App() {
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to main content
      </a>
      <header>
        <p>quantweave</p>
      </header>
      <nav aria-label="Primary">
        <ul>
          {SECTIONS.map((name) => (
            <li key={name}>
              <a href={`#/${name.toLowerCase()}`}>{name}</a>
            </li>
          ))}
        </ul>
      </nav>
      <main id="main" tabIndex={-1}>
        <h1>Portfolio Intelligence</h1>
        <p>
          This build is not connected to an API, so there is no portfolio data to show. Screens
          are added by their own tasks.
        </p>
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
