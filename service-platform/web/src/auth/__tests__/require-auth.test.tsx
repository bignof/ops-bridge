import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import { AuthProvider } from '../AuthContext';
import RequireAuth from '../RequireAuth';

// B8:RequireAuth 是 RCE 控制台的路由守卫主闸 —— 无 token 必须重定向到 /login,
// 且把原目标塞进 location.state.from(登录后跳回)。此前无任何 vitest 覆盖该闸。

// 登录页桩:把收到的 state.from 显示出来,断言重定向确实带上了原目标。
function LoginStub() {
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? '(无)';
  return <div>登录页 from={from}</div>;
}

function renderAt(path: string) {
  return render(
    <AuthProvider>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/login" element={<LoginStub />} />
          <Route
            path="/secret"
            element={
              <RequireAuth>
                <div>机密页面</div>
              </RequireAuth>
            }
          />
        </Routes>
      </MemoryRouter>
    </AuthProvider>,
  );
}

describe('RequireAuth 路由守卫(B8)', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  it('无 token → 重定向到 /login,且 state.from 记录原目标', async () => {
    renderAt('/secret');

    // 守卫拦截:渲染的是登录页而非机密页面。
    expect(await screen.findByText(/登录页/)).toBeInTheDocument();
    expect(screen.queryByText('机密页面')).not.toBeInTheDocument();
    // from 带上了被拦截的原始路径(登录成功后据此跳回)。
    expect(screen.getByText(/from=\/secret/)).toBeInTheDocument();
  });

  it('有 token → 放行,渲染受保护内容', async () => {
    sessionStorage.setItem('platform_token', 'tok-ok');
    renderAt('/secret');

    expect(await screen.findByText('机密页面')).toBeInTheDocument();
    expect(screen.queryByText(/登录页/)).not.toBeInTheDocument();
  });
});
