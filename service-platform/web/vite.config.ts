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
    // 评审 D3(系统性根因:此前 vitest run 无 thresholds → 零覆盖文件永不让 CI 转红,假绿是结构必然)。
    // 覆盖率门:v8 provider;阈值取当前实测水位整数下限(实测 lines/statements 90.97、functions 89.15、
    // branches 84.95,均稳定;下取整 + 1pt 余量 → 90/89/84),锁住覆盖率防回退又不卡边(对齐后端
    // --cov-fail-under 思路)。注:resources.ts 是被各页 mock 的薄 client 包装层(0% 行覆盖,运行期无
    // 字段映射逻辑,映射仅在编译期 TS interface),纳入 include 后整体水位仍 90%+;它若回退引入运行
    // 逻辑会拉低覆盖率而被门挡住。
    coverage: {
      provider: 'v8',
      include: ['src/**'],
      // 排除:bootstrap 入口(main.tsx,挂 React 根,非单测目标)、类型声明、测试/测试基座本身。
      exclude: ['src/main.tsx', 'src/vite-env.d.ts', 'src/test/**', '**/__tests__/**'],
      reporter: ['text', 'json-summary'],
      thresholds: {
        lines: 90,
        functions: 89,
        branches: 84,
        statements: 90,
      },
    },
  },
});
