import { useState, useEffect, useCallback } from 'react';
import { fetchConnectors, fetchSettings } from '../api/client';
import type { ConnectorRow, UserSettings } from '../api/types';
import { useAppConfig } from '../contexts/AppConfigContext';
import { useAuth } from '../contexts/AuthContext';
import { useGuides } from '../contexts/GuidesContext';
import { useNavigationState } from '../contexts/NavigationContext';
import { useIsMobile } from '../hooks/useIsMobile';
import {
  DataConnectionsSection,
  MemoriesSection,
  AppearanceSection,
  GuidesSection,
  SkillsSection,
  SlackSection,
  GmailSection,
  SmsMessagesSection,
  InferenceApiSection,
  FeatureGatesSection,
  ServiceCredentialsSection,
  InferenceProvidersSection,
  PasswordSection,
  SignInSection,
  ModelSelectionSection,
  SignOutSection,
  AboutSection,
} from './settings';
import { ModalShell } from './ModalShell';
import './SettingsModal.css';

interface SettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
}

type SettingsSection = 'data-connections' | 'appearance' | 'password' | 'sign-in' | 'slack' | 'gmail' | 'sms' | 'memories' | 'guides' | 'skills' | 'inference-api' | 'feature-gates' | 'service-credentials' | 'inference-providers' | 'model-selection' | 'about' | 'sign-out';

interface SectionEntry {
  id: SettingsSection;
  label: string;
}

// Server-global feature gate (config/feature_gates.py) that keeps the
// deprecated Guides section alive for installs still migrating to Skills.
const GUIDES_FEATURE = 'guides';

const MAIN_SECTIONS: SectionEntry[] = [
  { id: 'data-connections', label: 'Data Connections' },
  { id: 'appearance', label: 'Appearance' },
  // Listed only under email/password sign-in.
  { id: 'password', label: 'Password' },
  { id: 'memories', label: 'Memories' },
  // Listed only while the admin `guides` feature gate is on for this user.
  { id: 'guides', label: 'Guides' },
  { id: 'skills', label: 'Skills' },
  { id: 'inference-api', label: 'Inference API' },
];

// Per-connector settings sections, listed after the main sections and only
// while GET /connectors says the backing service is usable for this user:
// Slack and Gmail (Gmail rides on the Google Services row) require the
// user's own connection, since their settings are meaningless without it;
// SMS Messages belongs to the Twilio plugin and is listed while the `twilio`
// row is available (plugin loaded + admin-configured), since its data lives
// behind the plugin's own /auth/twilio/templates routes.
interface ConnectorSectionEntry extends SectionEntry {
  service: string;
  requires: 'connected' | 'available';
}

const CONNECTOR_SECTIONS: ConnectorSectionEntry[] = [
  { id: 'slack', label: 'Slack', service: 'slack', requires: 'connected' },
  { id: 'gmail', label: 'Gmail', service: 'google_services', requires: 'connected' },
  { id: 'sms', label: 'SMS Messages', service: 'twilio', requires: 'available' },
];

function isConnectorSectionVisible(entry: ConnectorSectionEntry, connectors: ConnectorRow[]): boolean {
  const row = connectors.find((c) => c.service === entry.service);
  if (!row || row.available === false) return false;
  return entry.requires === 'available' || row.connected;
}

// Admin sections are desktop-only: the mobile settings takeover never shows them.
const ADMIN_SECTIONS: SectionEntry[] = [
  { id: 'sign-in', label: 'Sign-in' },
  { id: 'feature-gates', label: 'Features' },
  { id: 'service-credentials', label: 'Service Credentials' },
  { id: 'inference-providers', label: 'Inference Providers' },
  { id: 'model-selection', label: 'Model Selection' },
];

// Listed after every other section in the top nav group (below Admin for
// admins), but not pinned to the bottom like Sign Out.
const ABOUT_SECTION: SectionEntry = { id: 'about', label: 'About' };

const SIGN_OUT_SECTION: SectionEntry = { id: 'sign-out', label: 'Sign Out' };

const SECTION_LABELS = Object.fromEntries(
  [...MAIN_SECTIONS, ...CONNECTOR_SECTIONS, ...ADMIN_SECTIONS, ABOUT_SECTION, SIGN_OUT_SECTION].map((s) => [s.id, s.label])
) as Record<SettingsSection, string>;

export function SettingsModal({ isOpen, onClose }: SettingsModalProps) {
  const { isAdmin, enabledFeatures, refreshConnectionStatus } = useAuth();
  const { loginMethod } = useAppConfig();
  const { loadGuides: refreshContextGuides } = useGuides();
  const { settingsInitialSection, setSettingsInitialSection } = useNavigationState();
  const guidesEnabled = enabledFeatures.includes(GUIDES_FEATURE);
  const passwordLogin = loginMethod === 'password';
  const visibleMainSections = MAIN_SECTIONS.filter(
    (entry) =>
      (entry.id !== 'guides' || guidesEnabled) &&
      (entry.id !== 'password' || passwordLogin)
  );
  const isMobile = useIsMobile();
  const [activeSection, setActiveSection] = useState<SettingsSection>('data-connections');
  // Mobile two-tier nav: false shows the section list, true slides the active
  // section's content over it. Ignored on desktop (side-by-side layout).
  const [mobileSectionOpen, setMobileSectionOpen] = useState(false);
  const [, setSettings] = useState<UserSettings>({});
  const [isLoading, setIsLoading] = useState(true);
  const [connectors, setConnectors] = useState<ConnectorRow[]>([]);

  const visibleConnectorSections = CONNECTOR_SECTIONS.filter((entry) =>
    isConnectorSectionVisible(entry, connectors)
  );
  const isConnectorSectionActive = (id: SettingsSection) =>
    visibleConnectorSections.some((entry) => entry.id === id);

  // Connector-gated sections follow the live connection state: reload the
  // rows whenever Data Connections reports a connect/disconnect (the same
  // callback that refreshes the context's connection flags).
  const loadConnectors = useCallback(async () => {
    try {
      const response = await fetchConnectors();
      setConnectors(response.connectors);
    } catch (error) {
      console.error('Failed to load connectors:', error);
    }
  }, []);

  const handleConnectionChange = useCallback(() => {
    refreshConnectionStatus();
    loadConnectors();
  }, [refreshConnectionStatus, loadConnectors]);

  // Load settings when modal opens
  useEffect(() => {
    if (!isOpen) return;

    // Handle initial section navigation from context (e.g., from guide edit button)
    if (settingsInitialSection) {
      setActiveSection(settingsInitialSection as SettingsSection);
      setMobileSectionOpen(true);
      setSettingsInitialSection(null);
    }

    const loadSettings = async () => {
      setIsLoading(true);
      try {
        const response = await fetchSettings();
        setSettings(response.settings);
      } catch (error) {
        console.error('Failed to load settings:', error);
      } finally {
        setIsLoading(false);
      }
    };

    loadSettings();
    loadConnectors();
  }, [isOpen, settingsInitialSection, setSettingsInitialSection, loadConnectors]);

  // A connector section that lost its connection (e.g. Slack disconnected
  // from another tab) must not stay selected as an empty pane: fall back to
  // Data Connections, derived rather than stored so no effect re-renders.
  // The Guides section is feature-gated the same way: if the gate closes
  // while it is selected, fall back rather than render an empty pane.
  const isGatedSection = CONNECTOR_SECTIONS.some((entry) => entry.id === activeSection);
  const effectiveSection: SettingsSection =
    (isGatedSection && !isConnectorSectionActive(activeSection)) ||
    (activeSection === 'guides' && !guidesEnabled) ||
    (activeSection === 'password' && !passwordLogin)
      ? 'data-connections'
      : activeSection;

  // Reset the mobile nav to the section list for the next open
  useEffect(() => {
    if (!isOpen) setMobileSectionOpen(false);
  }, [isOpen]);

  // Escape on mobile first steps back to the section list
  const handleEscape = useCallback(() => {
    if (isMobile && mobileSectionOpen) {
      setMobileSectionOpen(false);
    } else {
      onClose();
    }
  }, [isMobile, mobileSectionOpen, onClose]);

  const openSection = (section: SettingsSection) => {
    setActiveSection(section);
    setMobileSectionOpen(true);
  };

  const renderActiveSection = () => {
    if (isLoading) {
      return <div className="settings-loading">Loading settings...</div>;
    }

    switch (effectiveSection) {
      case 'data-connections':
        return <DataConnectionsSection refreshConnectionStatus={handleConnectionChange} />;
      case 'slack':
        return <SlackSection />;
      case 'gmail':
        return <GmailSection />;
      case 'sms':
        return <SmsMessagesSection />;
      case 'memories':
        return <MemoriesSection />;
      case 'appearance':
        return <AppearanceSection />;
      case 'guides':
        if (!guidesEnabled) return null;
        return (
          <GuidesSection
            onGuidesChanged={refreshContextGuides}
            onNavigateToSkills={() => setActiveSection('skills')}
          />
        );
      case 'skills':
        return <SkillsSection />;
      case 'inference-api':
        return <InferenceApiSection />;
      case 'password':
        return passwordLogin ? <PasswordSection /> : null;
      case 'sign-in':
        return isAdmin && !isMobile ? <SignInSection /> : null;
      case 'feature-gates':
        return isAdmin && !isMobile ? <FeatureGatesSection /> : null;
      case 'service-credentials':
        return isAdmin && !isMobile ? <ServiceCredentialsSection /> : null;
      case 'inference-providers':
        return isAdmin && !isMobile ? <InferenceProvidersSection /> : null;
      case 'model-selection':
        return isAdmin && !isMobile ? <ModelSelectionSection /> : null;
      case 'about':
        return <AboutSection />;
      case 'sign-out':
        return <SignOutSection />;
      default:
        return null;
    }
  };

  const renderNavItem = ({ id, label }: SectionEntry, extraClass = '') => (
    <button
      key={id}
      className={`settings-nav-item ${extraClass} ${!isMobile && effectiveSection === id ? 'active' : ''}`}
      onClick={() => openSection(id)}
    >
      <span>{label}</span>
      {isMobile && (
        <svg className="settings-nav-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="9 18 15 12 9 6"></polyline>
        </svg>
      )}
    </button>
  );

  const showMobileSection = isMobile && mobileSectionOpen;

  return (
    <ModalShell
      isOpen={isOpen}
      onClose={onClose}
      onEscape={handleEscape}
      overlayClassName={`settings-overlay ${isMobile ? 'settings-overlay-mobile' : ''}`}
      modalClassName={`settings-modal ${isMobile ? 'settings-modal-mobile' : ''} ${showMobileSection ? 'settings-mobile-section-open' : ''}`}
    >
      {/* Header */}
      <div className="settings-header">
        {showMobileSection && (
          <button className="settings-back-button" onClick={() => setMobileSectionOpen(false)} aria-label="Back to settings sections">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="15 18 9 12 15 6"></polyline>
            </svg>
          </button>
        )}
        <h2>{showMobileSection ? SECTION_LABELS[effectiveSection] : 'Settings'}</h2>
        <button className="settings-close-button" onClick={onClose} aria-label="Close settings">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
        </button>
      </div>

      <div className="settings-body">
        {/* Left sidebar (desktop) / first-tier section list (mobile) */}
        <nav className="settings-nav">
          <div className="settings-nav-top">
            {visibleMainSections.map((section) => renderNavItem(section))}
            {visibleConnectorSections.map((section) => renderNavItem(section))}
            {isAdmin && !isMobile && (
              <>
                <div className="settings-nav-section-header">Admin</div>
                {ADMIN_SECTIONS.map((section) => renderNavItem(section))}
              </>
            )}
            {renderNavItem(ABOUT_SECTION, 'settings-nav-about')}
          </div>
          <div className="settings-nav-bottom">
            {renderNavItem(SIGN_OUT_SECTION, 'settings-nav-signout')}
          </div>
        </nav>

        {/* Right content (desktop) / slide-over second tier (mobile) */}
        <div className="settings-content">
          {renderActiveSection()}
        </div>
      </div>
    </ModalShell>
  );
}
