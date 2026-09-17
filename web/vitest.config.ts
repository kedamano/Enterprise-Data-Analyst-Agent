import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

// 单元测试配置（与 vite dev 同源：同一套 alias + React 插件）
// 运行：npm run test:unit
// 入口：**/*.test.ts(x) 与 **/*.spec.ts(x)
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    css: false,
    // AuthCentre 的双滑块 transition 要等 500ms；unit 不真正过渡，用 fake timers 加速
    // 不 global 启用（避免污染其他测试），需要 fake timers 的 suite 自己 vi.useFakeTimers()
    clearMocks: true,
    unstubGlobals: true,
  },
});
