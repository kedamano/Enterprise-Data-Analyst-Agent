import { useState } from "react";
import { Download, Loader2, PackageOpen, X } from "lucide-react";
import { fetchManifest, type ExportManifest } from "@/lib/api";
import { AuthError } from "@/lib/auth";

/**
 * D51：导出预览 —— 打包之前先说清楚包里有什么。
 *
 * 断在哪：交付包（E5/03）只能**盲点**——不知道里面有几份 CSV、有没有图、脱敏了没，
 * 点完才发现不是自己要的。
 *
 * 关键纪律：清单与真实 zip 由后端**同一个构造函数**产出（`export._build_items`），
 * 所以"预览说 5 个文件 / 12 KB"就是"包里 5 个文件 / 12 KB"。
 * 下载链接的 `masked` 取自响应里的**实际生效值**，避免"看了脱敏版、下到原始版"。
 */
export function ExportPreview({ sessionId }: { sessionId: string }) {
  const [open, setOpen] = useState(false);
  const [manifest, setManifest] = useState<ExportManifest | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setOpen(true);
    if (manifest) return; // 已加载过就不重复请求（会话内容不会再变）
    setLoading(true);
    setError(null);
    try {
      setManifest(await fetchManifest(sessionId));
    } catch (e) {
      // 失败必须显示出来：静默的预览比没有预览更糟（用户以为"包里是空的"）
      setError(
        e instanceof AuthError
          ? "需要登录后才能查看导出内容"
          : e instanceof Error
            ? e.message
            : "导出预览失败",
      );
    } finally {
      setLoading(false);
    }
  };

  const fmtBytes = (n: number) => {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(2)} MB`;
  };

  if (!open) {
    return (
      <button
        onClick={load}
        aria-label="导出预览"
        className="inline-flex items-center gap-1 rounded-control border border-rule bg-white px-2 py-1 text-small text-ink-3 transition hover:border-rule-strong hover:text-ink"
      >
        <PackageOpen className="h-4 w-4" /> 导出预览
      </button>
    );
  }

  return (
    <div
      className="mt-2 w-full rounded-panel border border-rule bg-canvas p-3 text-small"
      data-testid="export-preview"
    >
      <div className="mb-2 flex items-center justify-between">
        <span className="inline-flex items-center gap-1 font-medium text-ink-2">
          <PackageOpen className="h-4 w-4" />
          交付包内容
          {manifest && (
            <span className="font-normal text-ink-3">
              （{manifest.files.length} 个文件 · {fmtBytes(manifest.total_bytes)}）
            </span>
          )}
        </span>
        <button
          onClick={() => setOpen(false)}
          aria-label="收起导出预览"
          className="text-ink-3 transition hover:text-ink-2"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {loading && (
        <div className="flex items-center gap-1.5 text-ink-3">
          <Loader2 className="h-4 w-4 animate-spin" /> 正在读取交付包清单…
        </div>
      )}

      {error && (
        <div role="alert" className="mb-2 rounded-control border border-danger bg-danger-soft px-2 py-1.5 text-danger">
          {error}
        </div>
      )}

      {manifest && (
        <>
          {manifest.masked ? (
            <div className="mb-2 rounded-control border border-verified bg-verified-soft px-2 py-1.5 text-verified">
              该包为 <strong>脱敏版</strong>
              （按你的角色策略处理数据列；位图无法逐像素脱敏，故不含图表）
            </div>
          ) : (
            <div className="mb-2 rounded-control border border-attention bg-attention-soft px-2 py-1.5 text-attention">
              该包为 <strong>原始值</strong>版本：对外分享前请自行确认是否含敏感数据
            </div>
          )}

          <ul className="mb-2 max-h-56 space-y-1 overflow-y-auto">
            {manifest.files.map((f) => (
              <li key={f.name} className="flex items-baseline justify-between gap-3">
                <span className="min-w-0 flex-1 truncate font-mono text-micro text-ink-2" title={f.name}>
                  {f.name}
                </span>
                {f.note ? <span className="shrink-0 text-ink-3">{f.note}</span> : null}
                <span className="shrink-0 tabular-nums text-ink-3">{fmtBytes(f.bytes)}</span>
              </li>
            ))}
          </ul>

          {/* masked 取自响应里的实际生效值 → 看了哪一份就下哪一份 */}
          <a
            href={`/api/v1/chat/analyze/export/${sessionId}?format=zip&masked=${manifest.masked ? 1 : 0}`}
            aria-label="下载交付包"
            className="inline-flex items-center gap-1 rounded-control border border-rule-strong bg-white px-2 py-1 font-medium text-ink-2 transition hover:border-rule"
          >
            <Download className="h-4 w-4" /> 下载交付包
          </a>
        </>
      )}
    </div>
  );
}
