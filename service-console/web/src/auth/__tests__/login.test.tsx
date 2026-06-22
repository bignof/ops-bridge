import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { AuthProvider } from '../AuthContext';
import LoginPage from '../LoginPage';

// mock api client:login 走 client.post('/auth/login') 返回 {token};
// AuthContext 的刷新保活会调 client.get('/auth/me'),一并 mock 防止 reject。
const post = vi.fn();
const get = vi.fn();
vi.mock('../../api/client', () => ({
  default: {
    post: (...args: unknown[]) => post(...args),
    get: (...args: unknown[]) => get(...args),
  },
}));

// antd Button 会在两个汉字间插入空格(「登录」→「登 录」),用规范化匹配抹掉空白再比对。
const loginButton = () =>
  screen.getByRole('button', {
    name: (name) => name.replace(/\s/g, '') === '登录',
  });

function renderLogin() {
  // '/' 落一个可识别的目标节点,断言登录成功后确实跳转过去。
  return render(
    <AuthProvider>
      <MemoryRouter initialEntries={['/login']}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/" element={<div>已进入控制台</div>} />
        </Routes>
      </MemoryRouter>
    </AuthProvider>,
  );
}

describe('LoginPage', () => {
  beforeEach(() => {
    post.mockReset();
    get.mockReset();
    get.mockResolvedValue({ data: { user: 'admin' } });
    sessionStorage.clear();
  });

  it('渲染登录表单(用户名/密码/登录按钮)', () => {
    renderLogin();
    expect(screen.getByLabelText('用户名')).toBeInTheDocument();
    expect(screen.getByLabelText('密码')).toBeInTheDocument();
    expect(loginButton()).toBeInTheDocument();
  });

  it('填表提交 → 调 /auth/login → 存 token 并跳转', async () => {
    post.mockResolvedValue({ data: { token: 'tok-123' } });
    const user = userEvent.setup();
    renderLogin();

    await user.type(screen.getByLabelText('用户名'), 'admin');
    await user.type(screen.getByLabelText('密码'), 'secret');
    await user.click(loginButton());

    // 调到了登录端点,且 body 为账号密码。
    await waitFor(() => {
      expect(post).toHaveBeenCalledWith('/auth/login', { username: 'admin', password: 'secret' });
    });
    // token 落 sessionStorage。
    await waitFor(() => {
      expect(sessionStorage.getItem('platform_token')).toBe('tok-123');
    });
    // 跳转到 '/'。
    expect(await screen.findByText('已进入控制台')).toBeInTheDocument();
  });

  it('401 → 提示用户名或密码错误,不存 token', async () => {
    post.mockRejectedValue({ response: { status: 401 } });
    const user = userEvent.setup();
    renderLogin();

    await user.type(screen.getByLabelText('用户名'), 'admin');
    await user.type(screen.getByLabelText('密码'), 'wrong');
    await user.click(loginButton());

    expect(await screen.findByText('用户名或密码错误')).toBeInTheDocument();
    expect(sessionStorage.getItem('platform_token')).toBeNull();
  });
});
