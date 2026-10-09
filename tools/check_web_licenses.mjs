#!/usr/bin/env node
// Licence gate for the pnpm workspace (ADR-013 licence policy, R022).
// Inventory: every package in pnpm-lock.yaml. Licences come from `pnpm licenses list --json`
// for installed packages and from OFF_PLATFORM for optional platform binaries that this host
// does not install. Fails on a denied, unknown or unreviewed licence, or on a lockfile package
// with no licence record. `--self-test` runs negative controls.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const ALLOWED = new Set([
  "Apache-2.0", "MIT", "BSD-2-Clause", "BSD-3-Clause", "ISC", "PSF-2.0", "Zlib",
  "PostgreSQL", "0BSD", "CC0-1.0",
]);
// Allowed with conditions: only for listed name@version, unmodified, never vendored, and
// never in the production (shipped) tree.
const CONDITIONAL = new Set(["MPL-2.0", "LGPL-3.0", "LGPL-3.0-only", "LGPL-3.0-or-later"]);
const DENIED = /GPL|SSPL|BUSL|Elastic|Commons[- ]Clause|non[- ]?commercial|UNLICENSED|Unknown|SEE LICENSE/i;
const LIGHTNINGCSS_PLATFORMS = [
  "android-arm64", "darwin-arm64", "darwin-x64", "freebsd-x64", "linux-arm-gnueabihf",
  "linux-arm64-gnu", "linux-arm64-musl", "linux-x64-gnu", "linux-x64-musl", "win32-arm64-msvc",
  "win32-x64-msvc",
];
const ROLLDOWN_PLATFORMS = [
  "android-arm-eabi", "android-arm64", "darwin-arm64", "darwin-x64", "freebsd-x64",
  "linux-arm-gnueabihf", "linux-arm64-gnu", "linux-arm64-musl", "linux-ppc64-gnu",
  "linux-s390x-gnu", "openharmony-arm64", "win32-arm64-msvc", "win32-x64-msvc",
];
// Package-scoped reviews. A version bump needs a new review. MIT-0 and BlueOak-1.0.0 are
// OSI-approved permissive licences outside the ADR-013 list: accepted per package by the
// orchestrator, dev only; owner confirmation is open.
export const REVIEWED = {
  "lightningcss@1.33.0": { license: "MPL-2.0", note: "vite CSS tooling; dev only, unmodified" },
  ...Object.fromEntries(LIGHTNINGCSS_PLATFORMS.map((p) => [
    `lightningcss-${p}@1.33.0`, { license: "MPL-2.0", note: "lightningcss native binary; dev only" },
  ])),
  "@csstools/color-helpers@6.1.2": { license: "MIT-0", note: "jsdom dependency; dev only" },
  "@csstools/css-syntax-patches-for-csstree@1.1.15": { license: "MIT-0", note: "jsdom dependency; dev only" },
  "lru-cache@11.5.3": { license: "BlueOak-1.0.0", note: "dev only" },
  "minimatch@10.2.6": { license: "BlueOak-1.0.0", note: "dev only" },
};
// Licences of lockfile packages that are optional binaries for other platforms, read from
// the npm registry (`npm view <name>@<version> license`) on 2026-10-09. A lockfile package
// that is neither installed nor listed here fails the gate.
export const OFF_PLATFORM = {
  ...Object.fromEntries(LIGHTNINGCSS_PLATFORMS.map((p) => [`lightningcss-${p}@1.33.0`, "MPL-2.0"])),
  ...Object.fromEntries(ROLLDOWN_PLATFORMS.map((p) => [`@rolldown/binding-${p}@1.2.13`, "MIT"])),
  "fsevents@2.3.3": "MIT",
};

// Package ids (name@version) from the `packages:` section of a pnpm v9 lockfile.
export function lockfileIds(text) {
  const section = text.split(/^packages:\n/m)[1]?.split(/^\S/m)[0] ?? "";
  return new Set(
    [...section.matchAll(/^ {2}'?((?:@[^/\s]+\/)?[^@\s']+)@([^(:'\s]+)/gm)].map((m) => `${m[1]}@${m[2]}`),
  );
}

// Joins the lockfile with the installed inventory. Returns rows plus coverage problems.
export function coverage(lockIds, installedRows, offPlatform = OFF_PLATFORM) {
  const installed = new Map(installedRows.map((r) => [r.id, r]));
  const rows = [];
  const problems = [];
  for (const id of lockIds) {
    const row = installed.get(id);
    if (row) rows.push(row);
    else if (id in offPlatform) rows.push({ id, license: offPlatform[id], offPlatform: true });
    else problems.push({ id, reason: "in pnpm-lock.yaml but not installed and no OFF_PLATFORM licence" });
  }
  for (const id of installed.keys()) {
    if (!lockIds.has(id)) problems.push({ id, reason: "installed but missing from pnpm-lock.yaml" });
  }
  return { rows, problems };
}

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
    ["MPL-2.0", "lightningcss-darwin-arm64@1.33.0", false, true],
  ];
  let failures = 0;
  for (const [license, id, production, ok] of cases) {
    const reason = classify(license, id, production);
    if ((reason === null) !== ok) {
      failures += 1;
      console.error(`SELF-TEST FAIL: ${String(license)} ${id} prod=${String(production)} -> ${String(reason)}`);
    }
  }
  const lock = lockfileIds(
    "lockfileVersion: '9.0'\n\npackages:\n\n  '@a/b@1.0.0':\n    resolution: {}\n\n  c@2.0.0(d@1.0.0):\n" +
      "    x: y\n\n  e-linux@3.0.0:\n    os: [linux]\n\nsnapshots:\n\n  z@9.9.9: {}\n",
  );
  const lockCases = [
    [[...lock].join(" ") === "@a/b@1.0.0 c@2.0.0 e-linux@3.0.0", "lockfile parse"],
    [coverage(lock, [{ id: "@a/b@1.0.0" }, { id: "c@2.0.0" }], {}).problems.length === 1, "uninstalled, unrecorded"],
    [coverage(lock, [{ id: "@a/b@1.0.0" }, { id: "c@2.0.0" }], { "e-linux@3.0.0": "MIT" }).problems.length === 0, "off-platform recorded"],
    [coverage(lock, [{ id: "@a/b@1.0.0" }, { id: "c@2.0.0" }, { id: "e-linux@3.0.0" }, { id: "x@1" }], {}).problems.length === 1, "installed, not locked"],
  ];
  for (const [ok, label] of lockCases) {
    if (!ok) {
      failures += 1;
      console.error(`SELF-TEST FAIL: ${label}`);
    }
  }
  const total = cases.length + lockCases.length;
  console.log(`self-test: ${String(total - failures)}/${String(total)} cases as expected`);
  return failures === 0;
}

function main() {
  if (process.argv.includes("--self-test")) return selfTest();
  const installed = inventory([]);
  const production = new Set(inventory(["--prod"]).map((r) => r.id));
  const lockIds = lockfileIds(readFileSync(new URL("../pnpm-lock.yaml", import.meta.url), "utf8"));
  const { rows: all, problems } = coverage(lockIds, installed);
  for (const r of all) {
    const reason = classify(r.license, r.id, production.has(r.id));
    if (reason !== null) problems.push({ id: r.id, reason });
  }
  const manifest = JSON.parse(readFileSync(new URL("../apps/web/package.json", import.meta.url), "utf8"));
  const direct = { ...manifest.dependencies, ...manifest.devDependencies };
  console.log("direct dependencies of apps/web:");
  for (const [name, version] of Object.entries(direct)) {
    const row = all.find((r) => r.id === `${name}@${version}`);
    const scope = name in (manifest.dependencies ?? {}) ? "runtime" : "dev";
    console.log(`  ${name}@${version} ${row?.license ?? "NOT FOUND"} (${scope})`);
    if (!row) problems.push({ id: `${name}@${version}`, reason: "direct dependency missing from inventory" });
  }
  const counts = {};
  for (const r of all) counts[r.license] = (counts[r.license] ?? 0) + 1;
  const off = all.filter((r) => r.offPlatform).length;
  console.log(
    `pnpm-lock.yaml: ${String(all.length)} package versions (${String(all.length - off)} installed here, ` +
      `${String(off)} off-platform with registry-recorded licences, ${String(production.size)} production)`,
    counts,
  );
  for (const p of problems) console.error(`FAIL ${p.id}: ${p.reason}`);
  if (problems.length === 0) console.log("PASS: no denied, unknown or unreviewed licences");
  return problems.length === 0;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) process.exitCode = main() ? 0 : 1;
