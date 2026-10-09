import { describe, expect, it } from "vitest";
import { Linter } from "eslint";
import tseslint from "typescript-eslint";
import { moneyGuard } from "../eslint.config.js";
import {
  DecimalStringError,
  formatDecimal,
  isDecimalString,
  isProblem,
  parseDecimalString,
} from "./wire";

// Expected strings below are hand-computed, not produced by the implementation.
const fmt = (raw: string, fractionDigits?: number): string =>
  formatDecimal(
    parseDecimalString(raw),
    fractionDigits === undefined ? {} : { fractionDigits },
  );

describe("parseDecimalString", () => {
  it.each(["0", "1", "-1", "0.5", "-0.000000000000000001", "123.450"])(
    "accepts %s",
    (raw) => {
      expect(isDecimalString(raw)).toBe(true);
      expect(parseDecimalString(raw)).toBe(raw);
    },
  );

  it.each([
    "",
    "-0",
    "-0.0",
    "-0.000",
    "+1",
    "01",
    "-01",
    ".5",
    "1.",
    "1e5",
    "1E5",
    " 1",
    "1 ",
    "1\n",
    "1,000",
    "NaN",
    "Infinity",
    "-Infinity",
    "0x10",
    "١",
    "1".repeat(27),
    "0." + "1".repeat(19),
  ])("rejects %j", (raw) => {
    expect(isDecimalString(raw)).toBe(false);
    expect(() => parseDecimalString(raw)).toThrow(DecimalStringError);
  });

  it.each([1.5, 0, 10n, null, undefined, {}, ["1"]])(
    "rejects non-string input #%#",
    (raw) => {
      expect(isDecimalString(raw)).toBe(false);
      expect(() => parseDecimalString(raw)).toThrow(DecimalStringError);
    },
  );

  it("caps the input echoed in the error message", () => {
    const long = "x".repeat(500);
    expect(() => parseDecimalString(long)).toThrow(/^.{0,120}$/);
  });
});

describe("formatDecimal without rounding", () => {
  it.each([
    ["0", "0"],
    ["7", "7"],
    ["999", "999"],
    ["1000", "1,000"],
    ["-1000", "-1,000"],
    ["1234567.891", "1,234,567.891"],
    ["-1234.5", "-1,234.5"],
    ["-0.5", "-0.5"],
    ["123.450", "123.450"],
    ["0.000000000000000001", "0.000000000000000001"],
    // 2^53 + 1: a JS number would display ...992.
    ["9007199254740993", "9,007,199,254,740,993"],
    ["-9007199254740993.01", "-9,007,199,254,740,993.01"],
    [
      "99999999999999999999999999.999999999999",
      "99,999,999,999,999,999,999,999,999.999999999999",
    ],
    [
      "-12345678901234567890123456.000000000001",
      "-12,345,678,901,234,567,890,123,456.000000000001",
    ],
  ])("%s -> %s", (raw, expected) => {
    expect(fmt(raw)).toBe(expected);
  });
});

describe("formatDecimal with fractionDigits (display rounds half-even)", () => {
  it.each([
    ["1.005", 2, "1.00"],
    ["1.015", 2, "1.02"],
    ["1.025", 2, "1.02"],
    ["1.0051", 2, "1.01"],
    ["0.125", 2, "0.12"],
    ["0.135", 2, "0.14"],
    ["-2.675", 2, "-2.68"],
    ["-2.665", 2, "-2.66"],
    ["999.995", 2, "1,000.00"],
    ["-999999.5", 0, "-1,000,000"],
    ["2.5", 0, "2"],
    ["3.5", 0, "4"],
    ["0.5", 0, "0"],
    ["-0.004", 2, "0.00"],
    ["-0.005", 2, "0.00"],
    ["-0.015", 2, "-0.02"],
    ["5", 2, "5.00"],
    ["-12.3", 4, "-12.3000"],
    ["0", 3, "0.000"],
    ["1.10", 1, "1.1"],
    [
      "99999999999999999999999999.999999999999",
      2,
      "100,000,000,000,000,000,000,000,000.00",
    ],
    ["9007199254740993.125", 2, "9,007,199,254,740,993.12"],
  ] as const)("%s at %i -> %s", (raw, digits, expected) => {
    expect(fmt(raw, digits)).toBe(expected);
  });

  it.each([-1, 1.5, 19, Number.NaN])("rejects fractionDigits %s", (digits) => {
    expect(() => fmt("1", digits)).toThrow(RangeError);
  });
});

describe("formatDecimal separators", () => {
  it("uses the given group and decimal separators", () => {
    const value = parseDecimalString("-1234567.25");
    expect(
      formatDecimal(value, { groupSeparator: ".", decimalSeparator: "," }),
    ).toBe("-1.234.567,25");
    expect(formatDecimal(value, { groupSeparator: " " })).toBe(
      "-1 234 567.25",
    );
  });

  it("re-validates a value that was cast to the brand", () => {
    const forged = "1e3" as unknown as ReturnType<typeof parseDecimalString>;
    expect(() => formatDecimal(forged)).toThrow(DecimalStringError);
  });
});

// SYNTHETIC envelope following T008 review C-01 (project contract 0.2.0, T010).
const problem = {
  type: "/problems/revision_conflict",
  title: "Revision conflict",
  status: 409,
  code: "revision_conflict",
  detail: "The account changed since it was read.",
  correlation_id: "corr-SYNTHETIC-1",
  retryable: false,
  retry_after_s: null,
  field_errors: [
    { pointer: "/amount/amount", code: "decimal_scale_exceeded", message: "Too many decimals." },
  ],
  reasons: [
    { code: "stale_revision", message: "Re-read and retry.", affected_ids: ["acct-1"], blocking: true },
  ],
  error_schema: "1",
};

describe("isProblem", () => {
  it("accepts the C-01 envelope", () => {
    expect(isProblem(problem)).toBe(true);
    expect(isProblem({ ...problem, status: 429, retryable: true, retry_after_s: 30 })).toBe(true);
  });

  it.each([
    ["missing correlation_id", { ...problem, correlation_id: undefined }],
    ["0.1.0 trace_id shape", { ...problem, correlation_id: undefined, trace_id: "t-1" }],
    ["unknown error_schema", { ...problem, error_schema: "2" }],
    ["success status", { ...problem, status: 200 }],
    ["non-integer status", { ...problem, status: 409.5 }],
    ["status as string", { ...problem, status: "409" }],
    ["retryable as string", { ...problem, retryable: "false" }],
    ["negative retry_after_s", { ...problem, retry_after_s: -1 }],
    ["field error without pointer", { ...problem, field_errors: [{ code: "x", message: "m" }] }],
    ["reason without blocking", { ...problem, reasons: [{ code: "x", message: "m", affected_ids: [] }] }],
    ["field_errors not an array", { ...problem, field_errors: {} }],
    ["null", null],
    ["array", [problem]],
  ])("rejects %s", (_label, value) => {
    expect(isProblem(value)).toBe(false);
  });
});

describe("money guard lint rule", () => {
  const linter = new Linter({ configType: "flat" });
  const lint = (code: string): string[] =>
    linter
      .verify(
        code,
        [
          {
            files: ["**/*.ts"],
            languageOptions: { parser: tseslint.parser },
            rules: { "no-restricted-syntax": ["error", ...moneyGuard] },
          },
        ],
        "sample.ts",
      )
      .map((m) => m.message);

  // SYNTHETIC samples.
  it.each([
    "const a = parseFloat(amount);",
    "const a = parseInt(amount, 10);",
    "const a = Number(amount);",
    "const a = new Number(amount);",
    "const a = Number.parseFloat(amount);",
    "const a = Number.parseInt(amount);",
    "const f = Number.parseFloat; f(amount);",
    "const a = +amount;",
    "const a = +price.amount;",
  ])("fires on %s", (code) => {
    expect(lint(code).length).toBeGreaterThanOrEqual(1);
  });

  it("allows string and BigInt handling", () => {
    expect(
      lint("const n = BigInt(digits); const ok = Number.isInteger(status); const s = a + b;"),
    ).toEqual([]);
  });
});
