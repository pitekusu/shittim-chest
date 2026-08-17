import react from "@vitejs/plugin-react";
import { defineConfig } from "vite-plus";

export default defineConfig({
  plugins: [react()],
  fmt: {
    ignorePatterns: ["dist/**", "node_modules/**"],
    semi: true,
    singleQuote: false,
  },
  lint: {
    ignorePatterns: ["dist/**", "node_modules/**"],
    plugins: ["eslint", "typescript", "unicorn", "oxc", "react", "import", "jsx-a11y", "vitest"],
    options: {
      typeAware: true,
      typeCheck: true,
      maxWarnings: 0,
    },
  },
  test: {
    environment: "jsdom",
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
    setupFiles: ["./src/test/setup.ts"],
  },
});
