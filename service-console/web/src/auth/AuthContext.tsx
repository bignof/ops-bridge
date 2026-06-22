import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import client from '../api/client';

const TOKEN_KEY = 'platform_token';

export interface AuthContextValue {
  /** 当前会话 token(无则未登录)。 */
  token: string | null;
  /** 当前登录用户名(顶栏展示用),取自 /auth/me 或登录入参。 */
  user: string | null;
  /** 用账号密码登录:调 POST /auth/login,成功存 token 到 sessionStorage + context。 */
  login: (username: string, password: string) => Promise<void>;
  /** 登出:清 token + context。 */
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() => sessionStorage.getItem(TOKEN_KEY));
  const [user, setUser] = useState<string | null>(null);

  // 刷新保活:已有 token(如刷新页面后从 sessionStorage 恢复)时,best-effort 拉当前用户。
  // 401 由 client 响应拦截统一处理(清 token + 跳登录),这里静默吞掉即可。
  useEffect(() => {
    if (!token) {
      setUser(null);
      return;
    }
    let alive = true;
    client
      .get<{ user: string }>('/auth/me')
      .then((r) => {
        if (alive) setUser(r.data?.user ?? null);
      })
      .catch(() => {
        /* 401 已由拦截器处理;其它错误不阻塞 UI */
      });
    return () => {
      alive = false;
    };
  }, [token]);

  const login = useCallback(async (username: string, password: string) => {
    const r = await client.post<{ token: string }>('/auth/login', { username, password });
    const t = r.data.token;
    sessionStorage.setItem(TOKEN_KEY, t);
    setToken(t);
    // 先以登录入参作为展示名,/auth/me 回来后再校正(见 useEffect)。
    setUser(username);
  }, []);

  const logout = useCallback(() => {
    sessionStorage.removeItem(TOKEN_KEY);
    setToken(null);
    setUser(null);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ token, user, login, logout }),
    [token, user, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth 必须在 <AuthProvider> 内使用');
  return ctx;
}
