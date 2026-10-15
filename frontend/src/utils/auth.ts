/**
 * Authentication utilities
 */

/**
 * Check if user has a valid session by calling the /app/api/me endpoint.
 * Returns user info if authenticated, null otherwise.
 */
export async function checkSession(): Promise<{
  email: string;
  name: string;
  google_services_connected: boolean;
  has_any_service_connected: boolean;
  is_admin: boolean;
  is_impersonating: boolean;
  impersonator_email: string | null;
  impersonator_name: string | null;
  default_model: string | null;
  // Last-used model for public-project composers (null = never sent one).
  public_default_model?: string | null;
  // The thinking level each default was last sent with (null = the
  // model's default level).
  default_thinking_effort?: string | null;
  public_default_thinking_effort?: string | null;
  // Settings > Appearance colour scheme: "light" | "dark" | null (= auto).
  theme?: string | null;
  // Settings > Appearance colour theme id ("prototype" | "electric-blue" | "alloy" | "recall");
  // null = default (prototype).
  color_theme?: string | null;
  enabled_features?: string[];
  // Whether the account has an email/password sign-in password.
  has_password?: boolean;
} | null> {
  try {
    const response = await fetch('/app/api/me', {
      credentials: 'include',
    });
    if (response.ok) {
      return await response.json();
    }
    return null;
  } catch {
    return null;
  }
}
