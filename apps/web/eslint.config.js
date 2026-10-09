import js from "@eslint/js";
import { defineConfig } from "eslint/config";
import tseslint from "typescript-eslint";

// Money and quantities arrive as decimal strings (ADR-013, T008 review 3.2). Converting
// them to a JS number loses precision, so number coercion is banned in all of src: any
// reference to Number, parseFloat or parseInt (calls, aliases, `.map(Number)`, destructuring,
// Reflect.apply), except `Number.isInteger`/`Number.isSafeInteger`, plus the same names read
// from globalThis/window/self/global, and unary plus.
// Known gaps: `x * 1`, `x - 0`, Math.* on strings, computed names such as
// `globalThis["Num" + "ber"]`, and JSON numbers are not caught.
const coercion = "Decimal strings stay strings: format with ./wire, never coerce to a number.";
const NAMES = "/^(Number|parseFloat|parseInt)$/";
export const moneyGuard = [
  {
    selector:
      `Identifier[name=${NAMES}]` +
      ":not(MemberExpression[computed=false] > Identifier.property)" +
      ":not(TSPropertySignature[computed=false] > Identifier.key)" +
      ":not(ObjectExpression > Property[computed=false] > Identifier.key)" +
      ":not(MemberExpression[computed=false][property.name=/^is(Integer|SafeInteger)$/] > Identifier.object)",
    message: coercion,
  },
  {
    selector: `MemberExpression[object.name=/^(globalThis|window|self|global)$/][property.name=${NAMES}]`,
    message: coercion,
  },
  {
    selector: `MemberExpression[object.name=/^(globalThis|window|self|global)$/][property.value=${NAMES}]`,
    message: coercion,
  },
  { selector: "UnaryExpression[operator='+']", message: coercion },
];

export default defineConfig(
  { ignores: ["dist/", "node_modules/"] },
  js.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    extends: [...tseslint.configs.strictTypeChecked, ...tseslint.configs.stylisticTypeChecked],
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    rules: { "no-restricted-syntax": ["error", ...moneyGuard] },
  },
);
