/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// service-platform 控制台前端构建配置。
// - base '/' :SPA 与 FastAPI 同源托管(单容器),所有静态资源走根路径。
// - 使用 hash 路由(createHashRouter),刷新/深链不依赖后端 history fallback。
export default defineConfig({
  plugins: [react()],
  base: '/',
  server: {
    // 本地开发把 API 反代到 P1a 后端(默认 8080),前端走同源 '/' 调用即可命中。
    proxy: {
      '/api': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      '/auth': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      '/health': { target: 'http://127.0.0.1:8080', changeOrigin: true },
    },
  },
  build: {
    // Task 7 锁定方案①:产物直接落到 FastAPI StaticFiles 托管目录 `../app/static`,
    // 本地 `npm run build` 即就位(无需 docker 拷贝),uvicorn 起服务即可托管 SPA。
    // Dockerfile 多阶段则仍 COPY web/dist→app/static(node 阶段 outDir 仍是相对 /web 的
    // `../app/static` 会落到镜像内不可控位置,故镜像内构建用默认 dist 再 COPY,见 Dockerfile 注释)。
    outDir: '../app/static',
    // 清空旧产物,避免上次构建残留文件混入(StaticFiles 会原样托管)。
    emptyOutDir: true,
    // ⚠️ CSP 关键:关掉 modulepreload polyfill。Vite 默认会向 index.html 注入一段**内联**
    // <script> polyfill,会被 `script-src 'self'`(不放 unsafe-inline)直接挡掉 → 白屏。
    // 现代浏览器(Chrome/Edge/Firefox/Safari)均原生支持 modulepreload,本控制台为内网
    // 运维 admin,关掉 polyfill 安全且使产物**零内联脚本**,与 CSP 严格策略相容。
    modulePreload: { polyfill: false },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    css: false,
  },
});
