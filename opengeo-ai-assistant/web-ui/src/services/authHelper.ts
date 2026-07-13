// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.

// Stubbed authentication helpers for cloud-agnostic, no-auth setup.
// All functions return null or do nothing.

export function getApiKey(): string {
  return (
    (window as Window & { ENV?: { VITE_API_KEY?: string } }).ENV?.VITE_API_KEY ??
    import.meta.env.VITE_API_KEY ??
    ''
  );
}

export async function getAuthToken(): Promise<string | null> {
  // No authentication logic; always return null
  return null;
}

export async function refreshAuthToken(): Promise<string | null> {
  // No authentication logic; always return null
  return null;
}

export function clearCachedToken(): void {
  // No-op
}

/**
 * Get Authorization headers if a token is available.
 * Returns an empty object when no token exists (development mode / no auth).
 */
export async function getAuthHeaders(): Promise<Record<string, string>> {
  const token = await getAuthToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * Drop-in replacement for window.fetch that automatically attaches
 * the X-Api-Key header (when configured) and a Bearer token when available.
 *
 * Usage:  import { authenticatedFetch } from '../services/authHelper';
 *         const response = await authenticatedFetch(`${API_BASE_URL}/api/config`);
 */
export async function authenticatedFetch(
  input: RequestInfo | URL,
  init?: RequestInit
): Promise<Response> {
  const headers = new Headers(init?.headers);

  const apiKey = getApiKey();
  if (apiKey) headers.set('X-Api-Key', apiKey);

  const token = await getAuthToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);

  return fetch(input, { ...init, headers });
}

// ---- User Info (for account menu UI) ----

export interface UserInfo {
  name: string | null;
  email: string | null;
}

let cachedUser: UserInfo | null = null;

/**
 * Fetch the logged-in user's display name and email from EasyAuth.
 * Returns null in development mode or when EasyAuth is not configured.
 *
 * EasyAuth /.auth/me response shape:
 *   [{ id_token, user_claims: [{ typ, val }], ... }]
 * Common claim types:
 *   name                                        -> display name
 *   preferred_username / emails                  -> email
 *   http://schemas.xmlsoap.org/.../name          -> fallback name
 *   http://schemas.xmlsoap.org/.../emailaddress  -> fallback email
 */
export async function getUserInfo(): Promise<UserInfo | null> {
  // No authentication logic; always return null
  return null;
}
