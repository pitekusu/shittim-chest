import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "shittim-records-theme-v1";
export const THEME_MEDIA_QUERY = "(prefers-color-scheme: dark)";
export const THEME_COLORS: Readonly<Record<Theme, string>> = {
  light: "#f5fbff",
  dark: "#071724",
};

export function isTheme(value: unknown): value is Theme {
  return value === "light" || value === "dark";
}

export function resolveTheme(storedValue: string | null, prefersDark: boolean): Theme {
  if (isTheme(storedValue)) return storedValue;
  return prefersDark ? "dark" : "light";
}

function readStoredTheme(): Theme | null {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isTheme(value) ? value : null;
  } catch {
    return null;
  }
}

function preferredTheme(): Theme {
  return window.matchMedia(THEME_MEDIA_QUERY).matches ? "dark" : "light";
}

export function applyTheme(theme: Theme, documentRoot: Document = document): void {
  const root = documentRoot.documentElement;
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
  documentRoot
    .querySelector<HTMLMetaElement>('meta[name="theme-color"]')
    ?.setAttribute("content", THEME_COLORS[theme]);
}

export function useRecordsTheme(): {
  readonly theme: Theme;
  readonly toggleTheme: () => void;
} {
  const storedTheme = useRef<Theme | null>(readStoredTheme());
  const [theme, setTheme] = useState<Theme>(() => {
    const bootstrapTheme = document.documentElement.dataset.theme;
    if (isTheme(bootstrapTheme)) return bootstrapTheme;
    return storedTheme.current ?? preferredTheme();
  });

  useLayoutEffect(() => applyTheme(theme), [theme]);

  useEffect(() => {
    if (storedTheme.current !== null) return;
    const preference = window.matchMedia(THEME_MEDIA_QUERY);
    const followSystemTheme = (event: MediaQueryListEvent) => {
      if (storedTheme.current === null) setTheme(event.matches ? "dark" : "light");
    };
    preference.addEventListener("change", followSystemTheme);
    return () => preference.removeEventListener("change", followSystemTheme);
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme((currentTheme) => {
      const nextTheme = currentTheme === "dark" ? "light" : "dark";
      storedTheme.current = nextTheme;
      try {
        window.localStorage.setItem(THEME_STORAGE_KEY, nextTheme);
      } catch {
        // A blocked storage area must not prevent an in-memory theme change.
      }
      return nextTheme;
    });
  }, []);

  return { theme, toggleTheme };
}
