import { useState } from 'react';
import { useAppearance } from '../../contexts/AppearanceContext';
import { THEME_OPTIONS, type ThemePreference } from '../../utils/theme';
import { COLOR_THEMES, type ColorThemeId, type ColorThemeSpec } from '../../utils/colorTheme';
import './AppearanceSection.css';

const THEME_LABELS: Record<ThemePreference, string> = {
  light: 'Light',
  dark: 'Dark',
  auto: 'Auto',
};

const THEME_DESCRIPTIONS: Record<ThemePreference, string> = {
  light: 'Always use the light colour scheme.',
  dark: 'Always use the dark colour scheme.',
  auto: 'Follow your operating system setting.',
};

function ThemeIcon({ theme }: { theme: ThemePreference }) {
  const common = {
    width: 20,
    height: 20,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 2,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
  };
  switch (theme) {
    case 'light':
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
        </svg>
      );
    case 'dark':
      return (
        <svg {...common}>
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      );
    case 'auto':
      return (
        <svg {...common}>
          <rect x="2" y="3" width="20" height="14" rx="2" />
          <path d="M8 21h8M12 17v4" />
        </svg>
      );
  }
}

/**
 * Miniature "app" preview for a colour theme tile: nav strip on the raised
 * ground, chat ground behind it, an accent dot. Shows the dark and light
 * halves side by side so the tile reads the same in either scheme.
 */
function ColorThemeSwatch({ spec }: { spec: ColorThemeSpec }) {
  return (
    <span className="appearance-color-swatch" aria-hidden="true">
      {([0, 1] as const).map((i) => (
        <span
          key={i}
          className="appearance-color-swatch-half"
          style={{ backgroundColor: spec.swatch.surface[i] }}
        >
          <span
            className="appearance-color-swatch-nav"
            style={{ backgroundColor: spec.swatch.surfaceRaised[i] }}
          />
          <span
            className="appearance-color-swatch-accent"
            style={{ backgroundColor: spec.swatch.accent[i] }}
          />
        </span>
      ))}
    </span>
  );
}

/**
 * Settings > Appearance: light / dark / auto colour-scheme selector plus the
 * colour-theme (palette) picker. Both choices apply instantly
 * (AppearanceContext.setTheme / setColorTheme set the <html data-theme> /
 * <html data-color-theme> attributes and cache them) and are persisted
 * server-side via PUT /settings so they follow the user across devices.
 */
export function AppearanceSection() {
  const { theme, setTheme, colorTheme, setColorTheme } = useAppearance();
  const [error, setError] = useState<string | null>(null);

  const choose = async (next: ThemePreference) => {
    setError(null);
    try {
      await setTheme(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save theme');
    }
  };

  const chooseColorTheme = async (next: ColorThemeId) => {
    setError(null);
    try {
      await setColorTheme(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to save colour theme');
    }
  };

  return (
    <div className="settings-section appearance-section">
      <h3>Appearance</h3>
      <p className="settings-description">
        Choose how Quest looks. Auto follows your device's light or dark setting.
      </p>
      <div className="appearance-theme-options" role="radiogroup" aria-label="Theme">
        {THEME_OPTIONS.map((option) => (
          <button
            key={option}
            type="button"
            role="radio"
            aria-checked={theme === option}
            className={`appearance-theme-option ${theme === option ? 'selected' : ''}`}
            onClick={() => choose(option)}
          >
            <span className="appearance-theme-icon">
              <ThemeIcon theme={option} />
            </span>
            <span className="appearance-theme-label">{THEME_LABELS[option]}</span>
            <span className="appearance-theme-description">{THEME_DESCRIPTIONS[option]}</span>
          </button>
        ))}
      </div>

      <h4 className="appearance-subheading">Theme</h4>
      <p className="settings-description">
        Pick the colour palette: the accent colour and the backgrounds of the chat area and the
        sidebar, composer and side panels. Each theme has its own light and dark variant.
      </p>
      <div className="appearance-theme-options appearance-color-options" role="radiogroup" aria-label="Colour theme">
        {COLOR_THEMES.map((spec) => (
          <button
            key={spec.id}
            type="button"
            role="radio"
            aria-checked={colorTheme === spec.id}
            className={`appearance-theme-option ${colorTheme === spec.id ? 'selected' : ''}`}
            onClick={() => chooseColorTheme(spec.id)}
          >
            <ColorThemeSwatch spec={spec} />
            <span className="appearance-theme-label">{spec.label}</span>
            <span className="appearance-theme-description">{spec.description}</span>
          </button>
        ))}
      </div>
      {error && <p className="appearance-error" role="alert">{error}</p>}
    </div>
  );
}
