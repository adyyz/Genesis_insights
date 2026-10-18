import { deleteApp, initializeApp } from 'firebase/app';
import { beforeAuthStateChanged, browserPopupRedirectResolver, GoogleAuthProvider, initializeAuth, inMemoryPersistence, signInWithPopup, signOut } from 'firebase/auth';

export type GoogleSignIn = { signIn: (signal: AbortSignal) => Promise<string>; dispose: () => Promise<void> };

export function prepareGoogleSignIn(projectId: string, apiKey: string): GoogleSignIn {
  if (!/^[a-z][a-z0-9-]{4,28}[a-z0-9]$/.test(projectId) || projectId.startsWith('demo-') || !apiKey) {
    throw new Error('Google sign-in configuration is unavailable');
  }
  const app = initializeApp({ projectId, apiKey, authDomain: `${projectId}.firebaseapp.com` }, {
    name: `operator-${crypto.randomUUID()}`, automaticDataCollectionEnabled: false,
  });
  const auth = initializeAuth(app, { persistence: inMemoryPersistence, popupRedirectResolver: browserPopupRedirectResolver });
  const provider = new GoogleAuthProvider();
  provider.setCustomParameters({ prompt: 'select_account' });
  let closed = false;
  let disposal: Promise<void> | null = null;
  const unsubscribe = beforeAuthStateChanged(auth, user => {
    if (user && closed) throw new Error('Sign-in cancelled');
  });
  return {
    async signIn(signal) {
      if (closed || signal.aborted) throw new DOMException('Sign-in cancelled', 'AbortError');
      try {
        const result = await signInWithPopup(auth, provider);
        if (closed || signal.aborted) throw new DOMException('Sign-in cancelled', 'AbortError');
        const token = await result.user.getIdToken();
        if (closed || signal.aborted) throw new DOMException('Sign-in cancelled', 'AbortError');
        return token;
      } finally {
        // App retains only the server-approved ID token; the SDK keeps no session.
        await signOut(auth).catch(() => {});
      }
    },
    dispose() {
      closed = true;
      disposal ??= signOut(auth).catch(() => {}).then(() => deleteApp(app)).catch(() => {}).finally(unsubscribe);
      return disposal;
    },
  };
}

export function googleSignInError(error: unknown): string {
  const value = (error && typeof error === 'object' ? error : {}) as { code?: string; status?: number; name?: string };
  if (value.status === 403) return 'This Google account is not approved for operator access. Use your approved account or contact the administrator.';
  if (value.status === 401) return 'Your sign-in expired or could not be verified. Sign in with Google again.';
  if (value.code === 'auth/popup-blocked') return 'Your browser blocked the sign-in window. Allow pop-ups for this site and try again.';
  if (['auth/popup-closed-by-user', 'auth/cancelled-popup-request'].includes(value.code || '')) return 'Google sign-in was cancelled. Try again when you are ready.';
  if (['auth/unauthorized-domain', 'auth/operation-not-allowed', 'auth/invalid-api-key', 'auth/auth-domain-config-required'].includes(value.code || '')) return 'Google sign-in is not configured for this site. Contact the administrator.';
  if (value.code === 'auth/network-request-failed' || value.name === 'TimeoutError' || error instanceof TypeError) return 'Unable to reach Google sign-in. Check your connection and retry.';
  return 'Google sign-in failed. Try again or contact the administrator.';
}
