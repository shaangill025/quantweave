// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { App } from "./main";
import indexHtml from "../index.html?raw";

afterEach(cleanup);

// Smoke checks for structure only; this is not an accessibility conformance assessment.
describe("app shell landmarks", () => {
  it("starts with a skip link that targets the focusable main region", () => {
    const { container } = render(<App />);
    const focusable = container.querySelectorAll("a[href], button, input, [tabindex='0']");
    const skip = screen.getByRole("link", { name: "Skip to main content" });
    expect(focusable[0]).toBe(skip);
    expect(skip.getAttribute("href")).toBe("#main");
    const main = screen.getByRole("main");
    expect(main.id).toBe("main");
    expect(main.getAttribute("tabindex")).toBe("-1");
  });

  it("has exactly one banner, primary navigation and main", () => {
    render(<App />);
    expect(screen.getByRole("note").textContent).toMatch(/never places, changes or cancels broker orders/);
    expect(screen.getAllByRole("banner")).toHaveLength(1);
    expect(screen.getAllByRole("navigation")).toHaveLength(1);
    expect(screen.getAllByRole("main")).toHaveLength(1);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("lists the spec 13 primary navigation in order", () => {
    render(<App />);
    const nav = screen.getByRole("navigation", { name: "Primary" });
    const names = within(nav)
      .getAllByRole("link")
      .map((a) => a.textContent);
    expect(names).toEqual([
      "Overview",
      "Accounts",
      "Decisions",
      "Watchlists",
      "Research",
      "Strategies",
      "Simulation",
      "Improvement",
      "Settings",
    ]);
  });

  it("says plainly that the API is unreachable instead of showing data", async () => {
    render(<App fetcher={() => Promise.reject(new TypeError("offline"))} />);
    const main = screen.getByRole("main");
    await waitFor(() => {
      expect(main.textContent).toContain("Proposals: Unavailable (api_unreachable)");
    });
    expect(main.textContent).toContain("Accounts: Unavailable (api_unreachable)");
    expect(main.textContent).not.toContain("Nothing in the loaded records needs attention");
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("declares a document language and title", () => {
    expect(indexHtml).toMatch(/<html lang="en">/);
    expect(indexHtml).toMatch(/<title>quantweave<\/title>/);
  });
});
