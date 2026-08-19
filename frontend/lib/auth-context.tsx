"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import { auth, type User } from "@/lib/api";

type AuthState = {
  user: User | null;
  /** True until the initial session restore finishes. */
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (input: {
    email: string;
    password: string;
    full_name: string;
    workspace_name?: string;
  }) => Promise<void>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
};

const AuthContext = React.createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [user, setUser] = React.useState<User | null>(null);
  const [loading, setLoading] = React.useState(true);

  // Restore the session once on mount. The access token lives in memory, so a
  // reload always starts without one; the httpOnly refresh cookie is what
  // survives, and this trades it for a fresh token before anything renders.
  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      const restored = await auth.bootstrap();
      if (!cancelled) {
        setUser(restored);
        setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = React.useCallback(
    async (email: string, password: string) => {
      setUser(await auth.login(email, password));
      router.push("/dashboard");
    },
    [router],
  );

  const register = React.useCallback(
    async (input: {
      email: string;
      password: string;
      full_name: string;
      workspace_name?: string;
    }) => {
      setUser(await auth.register(input));
      router.push("/dashboard");
    },
    [router],
  );

  const logout = React.useCallback(async () => {
    await auth.logout();
    setUser(null);
    router.push("/login");
  }, [router]);

  const refreshUser = React.useCallback(async () => {
    try {
      setUser(await auth.me());
    } catch {
      setUser(null);
    }
  }, []);

  const value = React.useMemo(
    () => ({ user, loading, login, register, logout, refreshUser }),
    [user, loading, login, register, logout, refreshUser],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = React.useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}
