import js from "@eslint/js";
import { defineConfig } from "eslint/config";
import tseslint from "typescript-eslint";

// Money and quantities arrive as decimal strings (ADR-013, T008 review 3.2). Converting
// them to a JS number loses precision, so number coercion is banned in all of src.
// Known gaps: `x * 1`, `x - 0`, Math.* on strings and JSON numbers are not caught.
const coercion = "Decimal strings stay strings: format with ./wire, never coerce to a number.";
export const moneyGuard = [
  { selector: "CallExpression[callee.name=/^(parseFloat|parseInt|Number)$/]", message: coercion },
  { selector: "NewExpression[callee.name='Number']", message: coercion },
  {
    selector: "MemberExpression[object.name='Number'][property.name=/^parse(Float|Int)$/]",
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
