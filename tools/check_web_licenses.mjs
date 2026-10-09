#!/usr/bin/env node
// Licence gate for the pnpm workspace (ADR-013 licence policy, R022).
// Reads `pnpm licenses list --json` for the full tree and for production dependencies, and
// fails on a denied, unknown or unreviewed licence. `--self-test` runs negative controls.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

const ALLOWED = new Set([
  "Apache-2.0", "MIT", "BSD-2-Clause", "BSD-3-Clause", "ISC", "PSF-2.0", "Zlib",
  "PostgreSQL", "0BSD", "CC0-1.0",
]);
// Allowed with conditions: only for listed name@version, unmodified, never vendored, and
// never in the production (shipped) tree.
const CONDITIONAL = new Set(["MPL-2.0", "LGPL-3.0", "LGPL-3.0-only", "LGPL-3.0-or-later"]);
const DENIED = /GPL|SSPL|BUSL|Elastic|Commons[- ]Clause|non[- ]?commercial|UNLICENSED|Unknown|SEE LICENSE/i;
// Package-scoped reviews. A version bump needs a new review.
export const REVIEWED = {
  "lightningcss@1.33.0": { license: "MPL-2.0", note: "vite CSS tooling; dev only, unmodified" },
  "lightningcss-linux-x64-gnu@1.33.0": { license: "MPL-2.0", note: "lightningcss native binary; dev only" },
  "lightningcss-linux-x64-musl@1.33.0": { license: "MPL-2.0", note: "lightningcss native binary; dev only" },
  "@csstools/color-helpers@6.1.2": { license: "MIT-0", note: "MIT without attribution; jsdom dev only; not in ADR-013 list, owner to confirm" },
  "@csstools/css-syntax-patches-for-csstree@1.1.15": { license: "MIT-0", note: "as above" },
  "lru-cache@11.5.3": { license: "BlueOak-1.0.0", note: "permissive; dev only; not in ADR-013 list, owner to confirm" },
  "minimatch@10.2.6": { license: "BlueOak-1.0.0", note: "as above" },
};

// Returns null when acceptable, or the reason it is not.
export function classify(license, id, production, reviewed = REVIEWED) {
  const expr = (license ?? "").trim();
  if (expr === "") return "no licence declared";
  const review = reviewed[id];
  const isReviewed = review !== undefined && review.license === expr;
  const alternatives = expr.replace(/^\((.*)\)$/, "$1").split(/\s+OR\s+/);
  if (alternatives.some((alt) => /[()]/.test(alt))) {
    return isReviewed && !production ? null : `compound expression needs review: ${expr}`;
  }
  const verdicts = alternatives.map((alt) => {
    const terms = alt.split(/\s+AND\s+/);
    if (terms.some((t) => DENIED.test(t) && !CONDITIONAL.has(t))) return "denied";
    if (terms.every((t) => ALLOWED.has(t))) return "allowed";
    return "review";
  });
  if (verdicts.includes("allowed")) return null;
  if (verdicts.every((v) => v === "denied")) return `denied licence: ${expr}`;
  if (production) return `production dependency needs an ADR-013 allowed licence: ${expr}`;
  if (verdicts.includes("review") && isReviewed) return null;
  return `unreviewed licence ${expr}; add a package-scoped review to REVIEWED`;
}

function inventory(extraArgs) {
  const out = execFileSync("pnpm", ["licenses", "list", "--json", ...extraArgs], {
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  const byLicense = JSON.parse(out);
  const rows = [];
  for (const [license, pkgs] of Object.entries(byLicense)) {
    for (const pkg of pkgs) {
      for (const version of pkg.versions) rows.push({ id: `${pkg.name}@${version}`, name: pkg.name, version, license });
    }
  }
  return rows;
}

function selfTest() {
  const cases = [
    ["MIT", "a@1", true, true],
    ["(MIT OR GPL-3.0)", "a@1", true, true],
    ["GPL-3.0", "a@1", false, false],
    ["AGPL-3.0-only", "a@1", false, false],
    ["MIT AND GPL-2.0", "a@1", false, false],
    ["SSPL-1.0", "a@1", false, false],
    ["BUSL-1.1", "a@1", false, false],
    ["Unknown", "a@1", false, false],
    ["UNLICENSED", "a@1", false, false],
    ["", "a@1", false, false],
    [undefined, "a@1", false, false],
    ["MPL-2.0", "unlisted@1", false, false],
    ["MPL-2.0", "lightningcss@1.33.0", false, true],
    ["MPL-2.0", "lightningcss@1.33.0", true, false],
    ["MPL-2.0", "lightningcss@1.34.0", false, false],
    ["GPL-3.0", "lightningcss@1.33.0", false, false],
    ["LGPL-3.0-or-later", "psycopg-like@1", false, false],
    ["BlueOak-1.0.0", "minimatch@10.2.6", false, true],
    ["BlueOak-1.0.0", "minimatch@10.2.6", true, false],
    ["(MIT AND (GPL-3.0 OR BSD-3-Clause))", "a@1", false, false],
  ];
  let failures = 0;
  for (const [license, id, production, ok] of cases) {
    const reason = classify(license, id, production);
    if ((reason === null) !== ok) {
      failures += 1;
      console.error(`SELF-TEST FAIL: ${String(license)} ${id} prod=${String(production)} -> ${String(reason)}`);
    }
  }
  console.log(`self-test: ${String(cases.length - failures)}/${String(cases.length)} cases as expected`);
  return failures === 0;
}

function main() {
  if (process.argv.includes("--self-test")) return selfTest();
  const all = inventory([]);
  const production = new Set(inventory(["--prod"]).map((r) => r.id));
  const problems = all
    .map((r) => ({ ...r, reason: classify(r.license, r.id, production.has(r.id)) }))
    .filter((r) => r.reason !== null);
  const manifest = JSON.parse(readFileSync(new URL("../apps/web/package.json", import.meta.url), "utf8"));
  const direct = { ...manifest.dependencies, ...manifest.devDependencies };
  console.log("direct dependencies of apps/web:");
  for (const [name, version] of Object.entries(direct)) {
    const row = all.find((r) => r.name === name && r.version === version);
    const scope = name in (manifest.dependencies ?? {}) ? "runtime" : "dev";
    console.log(`  ${name}@${version} ${row?.license ?? "NOT FOUND"} (${scope})`);
    if (!row) problems.push({ id: `${name}@${version}`, reason: "direct dependency missing from inventory" });
  }
  const counts = {};
  for (const r of all) counts[r.license] = (counts[r.license] ?? 0) + 1;
  console.log(`full tree: ${String(all.length)} package versions (${String(production.size)} production)`, counts);
  for (const p of problems) console.error(`FAIL ${p.id}: ${p.reason}`);
  if (problems.length === 0) console.log("PASS: no denied, unknown or unreviewed licences");
  return problems.length === 0;
}

process.exitCode = main() ? 0 : 1;
