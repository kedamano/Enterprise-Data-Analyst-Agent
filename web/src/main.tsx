import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// 自托管 IBM Plex（拉丁 + 数字）。只引实际用到的字重，避免带上整个字库：
// 400 正文 · 500 小标题/按钮 · 600 标题 · 700 强调数字。
// 中文字形由系统字体承担，见 index.css 的 --font-sans 栈。
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/ibm-plex-sans/700.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";

import "./index.css";
import App from "./App.tsx";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
