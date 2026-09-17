// vitest 启动时注入：
// 1) 扩展 expect 的 DOM 断言（toBeInTheDocument / toHaveAttribute 等）
// 2) 补齐 jsdom 未实现的 Blob URL API —— Composer 等组件用 URL.createObjectURL
//    生成图片附件预览，缺了它相关用例会抛 "Cannot read properties of undefined"。
// vitest.config.ts 的 setupFiles 引用本文件
import "@testing-library/jest-dom/vitest";

const globalUrl = globalThis.URL as typeof URL & {
  createObjectURL?: (obj: Blob | MediaSource) => string;
  revokeObjectURL?: (url: string) => void;
};

// 直接覆盖：jsdom 里该方法要么缺失、要么在非 jsdom Blob 上会内部报错
globalUrl.createObjectURL = () => "blob:vitest-mock";
globalUrl.revokeObjectURL = () => {};
