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
    // TODO(Task 7): 构建集成时把 outDir 指向被 FastAPI StaticFiles 托管的目录
    // (计划二选一:① 直接 outDir 到 ../app/static;② 保持默认 dist,由 Dockerfile
    //  多阶段 COPY web/dist -> app/static)。本任务暂用默认 'dist',不锁定方案。
    outDir: 'dist',
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    css: false,
  },
});
