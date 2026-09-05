import react from "@vitejs/plugin-react";
import { defineConfig } from "vite-plus";

import { assertGeneratedValidatorCurrent } from "./scripts/generate-contract-validator.mjs";
import { codeSplittingModuleOwnershipGuard } from "./scripts/code-splitting-guard";

assertGeneratedValidatorCurrent();

export default defineConfig({
  plugins: [react(), codeSplittingModuleOwnershipGuard()],
  fmt: {
    ignorePatterns: ["dist/**", "node_modules/**", "src/generated/**"],
    semi: true,
    singleQuote: false,
  },
  lint: {
    ignorePatterns: ["dist/**", "node_modules/**", "src/generated/**"],
    plugins: ["eslint", "typescript", "unicorn", "oxc", "react", "import", "jsx-a11y", "vitest"],
    // React Compiler is not enabled. Preserve these existing DOM/query synchronization
    // paths during the toolchain upgrade; new files keep the compiler-readiness rules.
    overrides: [
      {
        files: ["src/theme.ts", "src/RouteMotion.tsx", "src/routes/RecordsHome.tsx"],
        rules: { "react/refs": "off" },
      },
      {
        files: [
          "src/RouteMotion.tsx",
          "src/components/VoteGraph.tsx",
          "src/routes/RecordsHome.tsx",
          "src/routes/MemorialPage.tsx",
          "src/components/AdminPromptManager.tsx",
        ],
        rules: { "react/set-state-in-effect": "off" },
      },
    ],
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
