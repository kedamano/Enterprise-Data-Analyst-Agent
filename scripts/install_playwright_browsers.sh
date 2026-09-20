#!/usr/bin/env bash
# ---------------------------------------------------------------
# install_playwright_browsers.sh
#
# 前端 Playwright Chromium 安装脚本 —— 本机 proxy 在死节点时，
# 用 PLAYWRIGHT_DOWNLOAD_HOST 指向 npmmirror 国内镜像绕开。
#
# 用法：
#   直接跑（自动带镜像）： ./scripts/install_playwright_browsers.sh
#   只用官方源：                    PLAYWRIGHT_DOWNLOAD_HOST="" ./scripts/install_playwright_browsers.sh
#   装齐 3 个浏览器（chromium/firefox/webkit）： ./scripts/install_playwright_browsers.sh --all
# ---------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/web"

: "${PLAYWRIGHT_DOWNLOAD_HOST:=https://npmmirror.com/mirrors/playwright}"

main() {
  local target="${1:-chromium}"

  if [ -n "$PLAYWRIGHT_DOWNLOAD_HOST" ]; then
    echo "[install_playwright_browsers] 使用镜像：$PLAYWRIGHT_DOWNLOAD_HOST"
  else
    echo "[install_playwright_browsers] 未设置镜像，走 Playwright 官方 CDN（需国际出口）"
  fi

  case "$target" in
    chromium)
      echo "[install_playwright_browsers] 安装 chromium …"
      PLAYWRIGHT_DOWNLOAD_HOST="$PLAYWRIGHT_DOWNLOAD_HOST" npx playwright install chromium
      ;;
    --all)
      echo "[install_playwright_browsers] 安装 chromium + firefox + webkit …"
      PLAYWRIGHT_DOWNLOAD_HOST="$PLAYWRIGHT_DOWNLOAD_HOST" npx playwright install
      ;;
    *)
      echo "不支持的目标：$target"
      exit 2
      ;;
  esac

  echo "[install_playwright_browsers] 完成。缓存位于："
  echo "  Linux/macOS : ~/.cache/ms-playwright/"
  echo "  Windows     : \$USERPROFILE\\AppData\\Local\\ms-playwright\\"
}

main "$@"
