/**
 * Settings > Appearance: colour scheme (light / dark / auto) and colour theme
 * (palette). Both boot from the localStorage cache (already applied pre-paint
 * by the index.html boot script), are re-synced from the GET /me session
 * snapshot once, and persist via PUT /settings.
 */

import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { applyTheme, cacheTheme, normalizeTheme, readCachedTheme, type ThemePreference } from '../utils/theme';
import { applyColorTheme, cacheColorTheme, normalizeColorTheme, readCachedColorTheme, type ColorThemeId } from '../utils/colorTheme';
import { useAuth } from './AuthContext';

export interface AppearanceContextValue {
  // Settings > Appearance colour scheme ("light" / "dark" / "auto"). Boots
  // from the localStorage cache (already applied pre-paint by index.html),
  // then re-synced from users.settings.theme on GET /me hydration. setTheme
  // applies it immediately, caches it, and persists via PUT /settings.
  theme: ThemePreference;
  setTheme: (theme: ThemePreference) => Promise<void>;
  // Settings > Appearance colour THEME (palette: accent + grounds, e.g.
  // "prototype" / "electric-blue" / "alloy" / "recall"), same boot/hydrate/persist cycle as
  // `theme` against users.settings.color_theme and <html data-color-theme>.
  colorTheme: ColorThemeId;
  setColorTheme: (theme: ColorThemeId) => Promise<void>;
}

const AppearanceContext = createContext<AppearanceContextValue | null>(null);

export function AppearanceProvider({ children }: { children: React.ReactNode }) {
  const { sessionSnapshot } = useAuth();
  const [theme, setThemeState] = useState<ThemePreference>(() => readCachedTheme());
  const [colorTheme, setColorThemeState] = useState<ColorThemeId>(() => readCachedColorTheme());

  // The server-stored values win over the localStorage cache (which only
  // exists to avoid a pre-paint flash on this device).
  useEffect(() => {
    if (!sessionSnapshot) return;
    const serverTheme = normalizeTheme(sessionSnapshot.theme);
    applyTheme(serverTheme);
    cacheTheme(serverTheme);
    setThemeState(serverTheme);
    const serverColorTheme = normalizeColorTheme(sessionSnapshot.color_theme);
    applyColorTheme(serverColorTheme);
    cacheColorTheme(serverColorTheme);
    setColorThemeState(serverColorTheme);
  }, [sessionSnapshot]);

  // Apply + cache the theme right away (no flash while the request is in
  // flight), then persist it server-side. Rejects on a failed PUT so the
  // Appearance section can surface the error; the local choice stays applied.
  const setTheme = useCallback(async (next: ThemePreference) => {
    applyTheme(next);
    cacheTheme(next);
    setThemeState(next);
    const response = await fetch('/app/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme: next }),
    });
    if (!response.ok) {
      throw new Error(`Failed to save theme (HTTP ${response.status})`);
    }
  }, []);

  const setColorTheme = useCallback(async (next: ColorThemeId) => {
    applyColorTheme(next);
    cacheColorTheme(next);
    setColorThemeState(next);
    const response = await fetch('/app/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ color_theme: next }),
    });
    if (!response.ok) {
      throw new Error(`Failed to save colour theme (HTTP ${response.status})`);
    }
  }, []);

  const value = useMemo<AppearanceContextValue>(
    () => ({ theme, setTheme, colorTheme, setColorTheme }),
    [theme, setTheme, colorTheme, setColorTheme],
  );

  return <AppearanceContext.Provider value={value}>{children}</AppearanceContext.Provider>;
}

export function useAppearance(): AppearanceContextValue {
  const context = useContext(AppearanceContext);
  if (!context) {
    throw new Error('useAppearance must be used within an AppearanceProvider');
  }
  return context;
}
