/**
 * Guide list (deprecated feature; still listed for the Settings Guides
 * section and the routine guide-override dropdowns). Per-conversation guide
 * selection was removed with the composer guide selector.
 *
 * Guides are behind the admin `guides` feature gate: GET /guides 403s while
 * it is closed for this user, so the list is only fetched while the gate is
 * on and dropped when it closes. Deleting the feature means deleting this
 * file.
 */

import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { fetchGuides } from '../api/client';
import type { Guide } from '../api/types';
import { useAuth } from './AuthContext';

export interface GuidesContextValue {
  guides: Guide[];
  guidesLoaded: boolean;
  loadGuides: () => Promise<void>;
}

const GuidesContext = createContext<GuidesContextValue | null>(null);

export function GuidesProvider({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, enabledFeatures } = useAuth();
  const [guides, setGuides] = useState<Guide[]>([]);
  const [guidesLoaded, setGuidesLoaded] = useState(false);

  const loadGuides = useCallback(async () => {
    try {
      const response = await fetchGuides();
      setGuides(response.guides);
      setGuidesLoaded(true);
    } catch (err) {
      console.error('Failed to load guides:', err);
    }
  }, []);

  const guidesEnabled = enabledFeatures.includes('guides');
  useEffect(() => {
    if (!isAuthenticated) return;
    if (guidesEnabled) {
      loadGuides();
    } else {
      setGuides([]);
      setGuidesLoaded(false);
    }
  }, [isAuthenticated, guidesEnabled, loadGuides]);

  const value = useMemo<GuidesContextValue>(
    () => ({ guides, guidesLoaded, loadGuides }),
    [guides, guidesLoaded, loadGuides],
  );

  return <GuidesContext.Provider value={value}>{children}</GuidesContext.Provider>;
}

export function useGuides(): GuidesContextValue {
  const context = useContext(GuidesContext);
  if (!context) {
    throw new Error('useGuides must be used within a GuidesProvider');
  }
  return context;
}
