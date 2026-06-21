/* eslint-env node */
// service-platform 控制台前端 ESLint 配置(eslint 8 经典 .eslintrc 格式)。
module.exports = {
  root: true,
  env: { browser: true, es2020: true, node: true },
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'plugin:react-hooks/recommended',
    'prettier',
  ],
  ignorePatterns: ['dist', '.eslintrc.cjs', 'coverage', 'vite.config.ts'],
  parser: '@typescript-eslint/parser',
  parserOptions: {
    ecmaVersion: 'latest',
    sourceType: 'module',
    ecmaFeatures: { jsx: true },
  },
  plugins: ['@typescript-eslint', 'react-refresh'],
  rules: {
    'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
    '@typescript-eslint/no-explicit-any': 'warn',
    '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
  },
  overrides: [
    {
      // Context 文件刻意把 Provider 组件与 useXxx hook 并置(idiomatic React Context 模式);
      // react-refresh 的「只导出组件」约束不适用,关掉以免误报(HMR 在本文件本就整体刷新)。
      files: ['src/**/*Context.tsx'],
      rules: { 'react-refresh/only-export-components': 'off' },
    },
  ],
};
