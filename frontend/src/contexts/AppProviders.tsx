/**
 * Composes the app-wide React contexts, each owning one responsibility so a
 * consumer only re-renders for the state it actually reads:
 *
 * - AppConfigContext        unauthenticated server config + redeploy poll
 * - AuthContext             session check, user identity, feature gates
 * - AppearanceContext       colour scheme / colour theme
 * - NavigationContext       active conversation, views, first-send hand-offs
 * - ProjectsContext         project list, active + drilled project
 * - GuidesContext           deprecated guide list
 * - ConversationModelsContext  default + per-conversation models, provider locks
 * - ConversationSkillsContext  queued / loaded skills per conversation
 * - FileBrowserStateContext    per-conversation file-browser path
 * - DownloadWarningContext     the hidden-data acknowledgement dialog every
 *                              workspace download goes through
 *
 * Order matters only where a provider reads another: Auth reads AppConfig;
 * Appearance, Projects, Guides and ConversationModels read Auth (and
 * ConversationModels reads AppConfig).
 */

import React from 'react';
import { AppConfigProvider } from './AppConfigContext';
import { AuthProvider } from './AuthContext';
import { AppearanceProvider } from './AppearanceContext';
import { NavigationProvider } from './NavigationContext';
import { ProjectsProvider } from './ProjectsContext';
import { GuidesProvider } from './GuidesContext';
import { ConversationModelsProvider } from './ConversationModelsContext';
import { ConversationSkillsProvider } from './ConversationSkillsContext';
import { FileBrowserStateProvider } from './FileBrowserStateContext';
import { DownloadWarningProvider } from './DownloadWarningContext';

export function AppProviders({ children }: { children: React.ReactNode }) {
  return (
    <AppConfigProvider>
      <AuthProvider>
        <AppearanceProvider>
          <NavigationProvider>
            <ProjectsProvider>
              <GuidesProvider>
                <ConversationModelsProvider>
                  <ConversationSkillsProvider>
                    <FileBrowserStateProvider>
                      <DownloadWarningProvider>
                        {children}
                      </DownloadWarningProvider>
                    </FileBrowserStateProvider>
                  </ConversationSkillsProvider>
                </ConversationModelsProvider>
              </GuidesProvider>
            </ProjectsProvider>
          </NavigationProvider>
        </AppearanceProvider>
      </AuthProvider>
    </AppConfigProvider>
  );
}
