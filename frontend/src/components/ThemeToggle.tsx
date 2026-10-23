import { useState } from "react";
import { Moon, Sun } from "lucide-react";
import { Button } from "@/components/ui/button";

const THEME_KEY = "ar-collections-theme";

/** Apply the saved theme before rendering the dashboard. Light is the default. */
export function initializeTheme() {
  let dark = false;
  try {
    dark = window.localStorage.getItem(THEME_KEY) === "dark";
  } catch {
    // Storage may be unavailable; the toggle still works for this visit.
  }
  document.documentElement.classList.toggle("dark", dark);
}

export function ThemeToggle() {
  const [dark, setDark] = useState(() => document.documentElement.classList.contains("dark"));
  const label = dark ? "Switch to light theme" : "Switch to dark theme";
  const toggleTheme = () => {
    const nextDark = !dark;
    document.documentElement.classList.toggle("dark", nextDark);
    setDark(nextDark);
    try {
      window.localStorage.setItem(THEME_KEY, nextDark ? "dark" : "light");
    } catch {
      // Keep the selected theme even when it cannot be saved.
    }
  };

  return (
    <Button type="button" variant="outline" size="sm" className="h-11 sm:h-9" onClick={toggleTheme} aria-label={label} title={label}>
      {dark ? <Sun aria-hidden="true" /> : <Moon aria-hidden="true" />}
      {dark ? "Light" : "Dark"}
    </Button>
  );
}
