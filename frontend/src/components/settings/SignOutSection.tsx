import { useState, useCallback } from 'react';
import { logout, logoutAndDisconnect, deleteAccount } from '../../api/client';
import { clearAllDocDrafts } from '../../utils/docDraftBackup';
import './SignOutSection.css';

export function SignOutSection() {
  const [isLoggingOut, setIsLoggingOut] = useState(false);

  const handleLogout = useCallback(async () => {
    setIsLoggingOut(true);
    try {
      await logout();
      // Unsaved Quest Docs drafts must not outlive the session in this browser.
      clearAllDocDrafts();
      window.location.href = '/';
    } catch (error) {
      console.error('Failed to logout:', error);
      setIsLoggingOut(false);
    }
  }, []);

  const handleLogoutAndDisconnect = useCallback(async () => {
    if (!confirm('Are you sure? This will revoke all connected service tokens (Google Services, Slack, Telegram, and connected API keys). Your conversations will be preserved.')) {
      return;
    }
    setIsLoggingOut(true);
    try {
      await logoutAndDisconnect();
      // Unsaved Quest Docs drafts must not outlive the session in this browser.
      clearAllDocDrafts();
      window.location.href = '/';
    } catch (error) {
      console.error('Failed to logout and disconnect:', error);
      setIsLoggingOut(false);
    }
  }, []);

  const handleDeleteAccount = useCallback(async () => {
    if (!confirm('Are you sure you want to permanently delete your account? This will remove ALL your data including conversations, connected services, and API keys. This cannot be undone.')) {
      return;
    }
    // Double confirmation for destructive action
    if (!confirm('This is irreversible. Type DELETE to confirm... (Click OK to proceed)')) {
      return;
    }
    setIsLoggingOut(true);
    try {
      await deleteAccount();
      // Unsaved Quest Docs drafts must not outlive the session in this browser.
      clearAllDocDrafts();
      window.location.href = '/';
    } catch (error) {
      console.error('Failed to delete account:', error);
      setIsLoggingOut(false);
    }
  }, []);

  return (
    <div className="settings-section">
      <h3>Sign Out</h3>
      <div className="sign-out-options">
        <div className="sign-out-option">
          <div className="sign-out-option-info">
            <h4>Logout</h4>
            <p className="settings-description">
              Sign out of your session. Your conversations and connected services will be preserved.
            </p>
          </div>
          <button
            className="sign-out-btn sign-out-btn-logout"
            onClick={handleLogout}
            disabled={isLoggingOut}
          >
            Logout
          </button>
        </div>

        <div className="sign-out-option">
          <div className="sign-out-option-info">
            <h4>Logout and Disconnect</h4>
            <p className="settings-description">
              Sign out and revoke all connected service tokens (Google Services, Slack, Telegram, and connected API keys). Your conversations will be preserved.
            </p>
          </div>
          <button
            className="sign-out-btn sign-out-btn-disconnect"
            onClick={handleLogoutAndDisconnect}
            disabled={isLoggingOut}
          >
            Logout & Disconnect
          </button>
        </div>

        <div className="sign-out-option">
          <div className="sign-out-option-info">
            <h4>Delete Account</h4>
            <p className="settings-description">
              Permanently delete your account and all associated data, including conversations, connected services, and API keys. This action cannot be undone.
            </p>
          </div>
          <button
            className="sign-out-btn sign-out-btn-delete"
            onClick={handleDeleteAccount}
            disabled={isLoggingOut}
          >
            Delete Account
          </button>
        </div>
      </div>
    </div>
  );
}
