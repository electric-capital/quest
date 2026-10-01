import { useState, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { triggerAdminShutdown, fetchAdminUsers, impersonateUser, stopImpersonation } from '../api/client';
import type { UserSearchResult } from '../api/types';
import './AdminOpsMenu.css';

interface AdminOpsMenuProps {
  /**
   * Inline mode renders the button as a sidebar icon (used in UserInfoBar,
   * next to the settings gear) instead of a floating fixed-position button.
   */
  inline?: boolean;
}

/**
 * Admin operations menu button.
 * Visible only to admin users (determined by isAdmin from context).
 * Rendered inline in the sidebar's UserInfoBar next to the settings gear;
 * pages without a sidebar (System Reports) use the floating variant, a
 * small fixed button in the bottom-right corner. Both open an upward
 * dropdown with server operations.
 */
export function AdminOpsMenu({ inline = false }: AdminOpsMenuProps = {}) {
  const { isAdmin, isImpersonating, userEmail } = useAuth();
  const navigate = useNavigate();
  const [isOpen, setIsOpen] = useState(false);
  const [isShuttingDown, setIsShuttingDown] = useState(false);
  const [confirmingShutdown, setConfirmingShutdown] = useState(false);
  const [showUserPicker, setShowUserPicker] = useState(false);
  const [userList, setUserList] = useState<UserSearchResult[]>([]);
  const [loadingUsers, setLoadingUsers] = useState(false);
  const [isStopping, setIsStopping] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // Close dropdown when clicking outside
  useEffect(() => {
    if (!isOpen) return;

    function handleClickOutside(event: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setIsOpen(false);
        setConfirmingShutdown(false);
        setShowUserPicker(false);
      }
    }

    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [isOpen]);

  if (!isAdmin && !isImpersonating) return null;

  const handleShutdownClick = async () => {
    if (!confirmingShutdown) {
      setConfirmingShutdown(true);
      return;
    }

    // Confirmed -- trigger shutdown
    try {
      setIsShuttingDown(true);
      setIsOpen(false);
      setConfirmingShutdown(false);
      await triggerAdminShutdown();
    } catch (err) {
      console.error('Failed to trigger shutdown:', err);
      setIsShuttingDown(false);
    }
  };

  const handleImpersonateClick = async () => {
    setLoadingUsers(true);
    try {
      const data = await fetchAdminUsers();
      setUserList(data.users);
      setShowUserPicker(true);
    } catch (err) {
      console.error('Failed to fetch users:', err);
    } finally {
      setLoadingUsers(false);
    }
  };

  const handleUserSelect = async (user: UserSearchResult) => {
    try {
      await impersonateUser(user.id);
      window.location.reload();
    } catch (err) {
      console.error('Failed to impersonate user:', err);
    }
  };

  const handleStopImpersonation = async () => {
    try {
      setIsStopping(true);
      await stopImpersonation();
      window.location.href = '/';
    } catch (err) {
      console.error('Failed to stop impersonation:', err);
      setIsStopping(false);
    }
  };

  const handleToggle = () => {
    if (isShuttingDown) return;
    setIsOpen((prev) => !prev);
    setConfirmingShutdown(false);
    setShowUserPicker(false);
  };

  return (
    <div className={`admin-ops-container${inline ? ' admin-ops-inline' : ''}`} ref={menuRef}>
      {isOpen && (
        <div className="admin-ops-dropdown">
          {isImpersonating && (
            <>
              <div className="admin-ops-impersonating-info">
                Viewing as <strong>{userEmail}</strong>
              </div>
              <button
                className="admin-ops-item admin-ops-item-stop"
                onClick={handleStopImpersonation}
                disabled={isStopping}
              >
                {isStopping ? 'Stopping...' : 'End impersonation'}
              </button>
            </>
          )}
          {showUserPicker ? (
            <>
              <button
                className="admin-ops-item admin-ops-item-back"
                onClick={() => setShowUserPicker(false)}
              >
                &larr; Back
              </button>
              <div className="admin-ops-user-list">
                {userList.map((u) => (
                  <button
                    key={u.id}
                    className="admin-ops-item admin-ops-user-item"
                    onClick={() => handleUserSelect(u)}
                  >
                    <span className="admin-ops-user-name">{u.name}</span>
                    <span className="admin-ops-user-email">{u.email}</span>
                  </button>
                ))}
                {userList.length === 0 && (
                  <div className="admin-ops-no-users">No other users found</div>
                )}
              </div>
            </>
          ) : isAdmin ? (
            <>
              <button
                className="admin-ops-item"
                onClick={() => {
                  setIsOpen(false);
                  navigate('/admin/system-reports');
                }}
              >
                System Reports
              </button>
              <button
                className="admin-ops-item"
                onClick={handleImpersonateClick}
                disabled={loadingUsers}
              >
                {loadingUsers ? 'Loading...' : 'Impersonate user'}
              </button>
              <button
                className={`admin-ops-item admin-ops-item-danger${confirmingShutdown ? ' confirming' : ''}`}
                onClick={handleShutdownClick}
              >
                {confirmingShutdown ? 'Are you sure?' : 'Shut down server'}
              </button>
            </>
          ) : null}
        </div>
      )}
      <button
        className={`admin-ops-button${isShuttingDown ? ' shutting-down' : ''}${isImpersonating ? ' impersonating' : ''}`}
        onClick={handleToggle}
        title={isShuttingDown ? 'Shutting down...' : isImpersonating ? `Impersonating ${userEmail}` : 'Admin operations'}
        aria-label={isImpersonating ? 'Impersonation active' : 'Admin operations'}
        disabled={isShuttingDown}
      >
        {isShuttingDown ? (
          <span className="admin-ops-spinner" />
        ) : (
          /* Wrench icon - inline SVG */
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"></path>
          </svg>
        )}
      </button>
    </div>
  );
}
