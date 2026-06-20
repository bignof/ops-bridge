import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

// 每个用例后卸载组件、清 sessionStorage,避免用例间状态串扰。
afterEach(() => {
  cleanup();
  sessionStorage.clear();
});

// jsdom 不实现 matchMedia,antd 部分组件(如响应式)依赖它,补一个最小 stub。
if (!window.matchMedia) {
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList;
}
