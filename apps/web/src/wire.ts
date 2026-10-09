// Wire types shared with apps/api. They are hand-written until the OpenAPI type generator is
// chosen (T008 review A-04), and they follow the T008 review resolutions for project contract
// 0.2.0 (owned by T010). The web app only displays values: it never computes amounts.

declare const decimalBrand: unique symbol;
/** A decimal string as sent by the API (T008 review 3.2). Never converted to a JS number. */
export type DecimalString = string & { readonly [decimalBrand]: true };

// Widest wire class bounds: 26 integer digits (MoneyAmount, Price, Quantity) and scale 18
// (FxRate, Ratio). Class-specific bounds are enforced by the API, not by the display layer.
export const MAX_INTEGER_DIGITS = 26;
export const MAX_SCALE = 18;
const DECIMAL_RE = new RegExp(
  `^(?!-0(?:\\.0+)?$)-?(?:0|[1-9][0-9]{0,${String(MAX_INTEGER_DIGITS - 1)}})(?:\\.[0-9]{1,${String(MAX_SCALE)}})?$`,
);

export class DecimalStringError extends Error {
  override name = "DecimalStringError";
}

function preview(raw: unknown): string {
  const text = typeof raw === "string" ? JSON.stringify(raw) : typeof raw;
  return text.length > 64 ? `${text.slice(0, 64)}…` : text;
}

export function isDecimalString(raw: unknown): raw is DecimalString {
  return typeof raw === "string" && DECIMAL_RE.test(raw);
}

export function parseDecimalString(raw: unknown): DecimalString {
  if (!isDecimalString(raw)) {
    throw new DecimalStringError(`not a wire decimal string: ${preview(raw)}`);
  }
  return raw;
}

export interface FormatOptions {
  /** Round half-even (display rounding, T008 review 3.2) or zero-pad to exactly this scale. */
  fractionDigits?: number;
  groupSeparator?: string;
  decimalSeparator?: string;
}

function group(digits: string, separator: string): string {
  const head = digits.length % 3 || 3;
  const parts = [digits.slice(0, head)];
  for (let i = head; i < digits.length; i += 3) parts.push(digits.slice(i, i + 3));
  return parts.join(separator);
}

function roundHalfEven(intDigits: string, frac: string, scale: number): [string, string] {
  const divisor = 10n ** BigInt(frac.length - scale);
  const scaled = BigInt(intDigits + frac);
  let quotient = scaled / divisor;
  const twiceRemainder = (scaled % divisor) * 2n;
  if (twiceRemainder > divisor || (twiceRemainder === divisor && quotient % 2n === 1n)) {
    quotient += 1n;
  }
  const digits = quotient.toString().padStart(scale + 1, "0");
  return [digits.slice(0, digits.length - scale), digits.slice(digits.length - scale)];
}

/**
 * Formats a wire decimal string with digit grouping, using only string and BigInt
 * operations. A value that rounds to zero is shown without a minus sign.
 */
export function formatDecimal(value: DecimalString, options: FormatOptions = {}): string {
  const { fractionDigits, groupSeparator = ",", decimalSeparator = "." } = options;
  if (
    fractionDigits !== undefined &&
    !(Number.isInteger(fractionDigits) && fractionDigits >= 0 && fractionDigits <= MAX_SCALE)
  ) {
    throw new RangeError(`fractionDigits must be an integer from 0 to ${String(MAX_SCALE)}`);
  }
  // A separator that is empty, contains a digit or a minus sign, or matches the other
  // separator would make the output ambiguous.
  if (
    [groupSeparator, decimalSeparator].some((sep) => sep === "" || /[\p{Nd}-]/u.test(sep)) ||
    groupSeparator === decimalSeparator
  ) {
    throw new RangeError("separators must be distinct, non-empty and contain no digit or minus");
  }
  const checked = parseDecimalString(value);
  const negative = checked.startsWith("-");
  const [intPart = "", fracPart = ""] = (negative ? checked.slice(1) : checked).split(".");
  let intDigits = intPart;
  let frac = fracPart;
  if (fractionDigits !== undefined) {
    if (frac.length > fractionDigits) {
      [intDigits, frac] = roundHalfEven(intDigits, frac, fractionDigits);
    } else {
      frac = frac.padEnd(fractionDigits, "0");
    }
  }
  const sign = negative && /[1-9]/.test(intDigits + frac) ? "-" : "";
  const fraction = frac === "" ? "" : decimalSeparator + frac;
  return sign + group(intDigits, groupSeparator) + fraction;
}

/** RFC 6901 pointer to the offending request field; messages never echo secret values. */
export interface FieldError {
  pointer: string;
  code: string;
  message: string;
}

export interface Reason {
  code: string;
  message: string;
  affected_ids: string[];
  blocking: boolean;
}

/** Versioned RFC 9457 error envelope (T008 review C-01; `correlation_id` replaces `trace_id`). */
export interface Problem {
  type: string;
  title: string;
  status: number;
  code: string;
  detail: string;
  correlation_id: string;
  retryable: boolean;
  retry_after_s: number | null;
  field_errors: FieldError[];
  reasons: Reason[];
  error_schema: "1";
}

type Fields = Record<string, unknown>;

function isRecord(value: unknown): value is Fields {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function allStrings(value: Fields, keys: readonly string[]): boolean {
  return keys.every((key) => typeof value[key] === "string");
}

function isFieldError(value: unknown): value is FieldError {
  return isRecord(value) && allStrings(value, ["pointer", "code", "message"]);
}

function isReason(value: unknown): value is Reason {
  return (
    isRecord(value) &&
    allStrings(value, ["code", "message"]) &&
    typeof value.blocking === "boolean" &&
    Array.isArray(value.affected_ids) &&
    value.affected_ids.every((id) => typeof id === "string")
  );
}

/** Runtime check for an `application/problem+json` body before the UI relies on it. */
export function isProblem(value: unknown): value is Problem {
  if (!isRecord(value)) return false;
  const { status, retry_after_s: retryAfter, field_errors: fieldErrors, reasons } = value;
  return (
    allStrings(value, ["type", "title", "code", "detail", "correlation_id"]) &&
    value.error_schema === "1" &&
    typeof status === "number" &&
    Number.isInteger(status) &&
    status >= 400 &&
    status <= 599 &&
    typeof value.retryable === "boolean" &&
    (retryAfter === null ||
      (typeof retryAfter === "number" && Number.isInteger(retryAfter) && retryAfter >= 0)) &&
    Array.isArray(fieldErrors) &&
    fieldErrors.every(isFieldError) &&
    Array.isArray(reasons) &&
    reasons.every(isReason)
  );
}
