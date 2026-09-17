import { useEffect, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Loader2,
  Pencil,
  Plug,
  Plus,
  RefreshCw,
  ServerCog,
  Trash2,
  Wrench,
} from "lucide-react";
import {
  createMcpServer,
  deleteMcpServer,
  fetchMcpServers,
  setMcpServerEnabled,
  testMcpServer,
  testMcpServerAdHoc,
  updateMcpServer,
  type MCPProbeResult,
  type MCPServer,
  type MCPServerForm,
  type MCPTransport,
} from "@/lib/api";

const TRANSPORT_META: Record<MCPTransport, { label: string; cls: string; hint: string }> = {
  stdio: {
    label: "stdio",
    cls: "bg-brand-soft text-brand",
    hint: "本地子进程（command + args）",
  },
  sse: {
    label: "SSE",
    cls: "bg-attention-soft text-attention",
    hint: "远端 Server-Sent Events（url）",
  },
  http: {
    label: "HTTP",
    cls: "bg-verified-soft text-verified",
    hint: "远端 Streamable HTTP（url）",
  },
};

/** 把"链接目标"渲染成一行可读文本（stdio 是命令行，其余是 URL）。 */
function targetOf(s: MCPServer): string {
  if (s.transport === "stdio") return [s.command, ...(s.args ?? [])].join(" ").trim();
  return s.url || "（未配置）";
}

/**
 * MCP 服务器页：管理**要去连接的外部** MCP server。
 *
 * 注意方向——本产品的只读工具「暴露出去」是另一件事（`/api/v1/mcp/tools`，D50）。
 * 这里管的是「我们要去连别人」：添加 context7 / fetch / time 这类 server，
 * 测试连通性、查看它们暴露了哪些工具（本轮不把外部工具接入 Agent 工具表）。
 */
export function McpView() {
  const [servers, setServers] = useState<MCPServer[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<MCPServer | "new" | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [probes, setProbes] = useState<Record<string, MCPProbeResult>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const load = () => {
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    fetchMcpServers(ctrl.signal)
      .then((r) => setServers(r.servers))
      .catch((e) => {
        if ((e as DOMException)?.name === "AbortError") return;
        setError((e as Error).message);
      })
      .finally(() => setLoading(false));
    return () => ctrl.abort();
  };

  useEffect(load, []);

  const toggle = async (s: MCPServer) => {
    setBusyId(s.id);
    setError(null);
    try {
      const updated = await setMcpServerEnabled(s.id, !s.enabled);
      setServers((prev) => (prev ?? []).map((x) => (x.id === s.id ? { ...x, ...updated } : x)));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (s: MCPServer) => {
    if (!window.confirm(`确定删除 MCP 服务器「${s.name}」吗？`)) return;
    setBusyId(s.id);
    setError(null);
    try {
      await deleteMcpServer(s.id);
      setServers((prev) => (prev ?? []).filter((x) => x.id !== s.id));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusyId(null);
    }
  };

  const probe = async (s: MCPServer) => {
    setBusyId(s.id);
    setError(null);
    try {
      const r = await testMcpServer(s.id);
      setProbes((prev) => ({ ...prev, [s.id]: r }));
      setExpanded((prev) => ({ ...prev, [s.id]: true }));
    } catch (e) {
      setProbes((prev) => ({
        ...prev,
        [s.id]: {
          ok: false,
          tools: [],
          tool_count: 0,
          error: (e as Error).message,
          error_kind: "connection",
          latency_ms: 0,
          target: targetOf(s),
        },
      }));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col bg-canvas">
      <div className="flex h-14 shrink-0 items-center gap-2.5 border-b border-rule bg-white px-6">
        <ServerCog className="h-5 w-5 text-brand" />
        <h1 className="text-heading font-semibold text-ink">MCP 服务器</h1>
        <span className="text-small text-ink-3">
          配置与管理外部 Model Context Protocol 服务器
        </span>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={() => setEditing("new")}
            className="inline-flex items-center gap-1.5 rounded-control bg-brand px-3.5 py-2 text-small font-medium text-white transition hover:bg-brand/90"
          >
            <Plus className="h-4 w-4" /> 添加
          </button>
          <button
            onClick={() => void load()}
            className="grid h-9 w-9 place-items-center rounded-control border border-rule text-ink-3 transition hover:bg-canvas hover:text-ink"
            title="刷新"
            aria-label="刷新 MCP 服务器列表"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[1000px] px-6 py-6">
          {error && (
            <div className="mb-5 flex items-center gap-2.5 rounded-panel border border-danger bg-danger-soft px-4 py-3 text-small text-danger">
              <AlertCircle className="h-4 w-4 shrink-0" /> {error}
            </div>
          )}

          <div className="mb-5 rounded-panel border border-rule bg-white px-4 py-3 text-small leading-relaxed text-ink-3">
            这里管理的是<b className="text-ink-2">要去连接的外部 MCP 服务器</b>
            （如 context7 / fetch / time）。添加后可「测试连接」并查看它们暴露的工具。
            <br />
            本轮暂不把外部工具接入 Agent 的调用链，仅供配置与连通性核验。
          </div>

          <div className="mb-3 flex items-center gap-2.5">
            <Plug className="h-4 w-4 text-ink-3" />
            <h2 className="text-body font-semibold text-ink">
              已配置服务器（{servers?.length ?? 0}）
            </h2>
          </div>

          {(servers ?? []).length === 0 && !loading ? (
            <div className="rounded-panel border border-dashed border-rule-strong bg-white px-6 py-12 text-center">
              <ServerCog className="mx-auto h-9 w-9 text-ink-3" />
              <p className="mt-3 text-body font-medium text-ink-2">还没有 MCP 服务器</p>
              <p className="mt-1.5 text-small text-ink-3">
                点击右上角「添加」接入外部 MCP 服务器。stdio 传输需填写启动命令，
                <br />
                SSE / HTTP 传输需填写 URL。
              </p>
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              {(servers ?? []).map((s) => {
                const tm = TRANSPORT_META[s.transport] ?? TRANSPORT_META.stdio;
                const p = probes[s.id];
                const open = expanded[s.id];
                return (
                  <div
                    key={s.id}
                    className={`rounded-panel border bg-white ${s.enabled ? "border-rule" : "border-rule opacity-70"}`}
                  >
                    <div className="flex items-center gap-3 px-5 py-4">
                      <button
                        onClick={() => setExpanded((prev) => ({ ...prev, [s.id]: !open }))}
                        className="grid h-7 w-7 shrink-0 place-items-center rounded-control text-ink-3 transition hover:bg-canvas hover:text-ink"
                        aria-label={open ? "收起" : "展开"}
                        aria-expanded={open}
                      >
                        {open ? (
                          <ChevronDown className="h-4 w-4" />
                        ) : (
                          <ChevronRight className="h-4 w-4" />
                        )}
                      </button>
                      <span className="grid h-9 w-9 shrink-0 place-items-center rounded-control bg-brand-soft text-brand">
                        <ServerCog className="h-4.5 w-4.5" />
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <p className="truncate text-body font-semibold text-ink" title={s.name}>
                            {s.name}
                          </p>
                          <span className={`rounded-control px-2 py-0.5 text-small font-medium ${tm.cls}`}>
                            {tm.label}
                          </span>
                          {!s.enabled && (
                            <span className="rounded-control bg-canvas px-2 py-0.5 text-small text-ink-3">
                              已停用
                            </span>
                          )}
                        </div>
                        <p className="mt-0.5 truncate font-mono text-small text-ink-3" title={targetOf(s)}>
                          {targetOf(s) || "（未配置）"}
                        </p>
                      </div>

                      <div className="flex shrink-0 items-center gap-1.5">
                        <button
                          onClick={() => void probe(s)}
                          disabled={busyId === s.id}
                          className="inline-flex items-center gap-1.5 rounded-control border border-rule bg-white px-2.5 py-1.5 text-small font-medium text-ink-2 transition hover:border-brand hover:text-brand disabled:opacity-50"
                          title="测试连接并拉取工具清单"
                        >
                          {busyId === s.id ? (
                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                          ) : (
                            <Plug className="h-3.5 w-3.5" />
                          )}
                          测试连接
                        </button>
                        <button
                          onClick={() => void toggle(s)}
                          disabled={busyId === s.id}
                          className={`rounded-control px-2.5 py-1.5 text-small font-medium transition disabled:opacity-50 ${
                            s.enabled ? "bg-verified-soft text-verified" : "bg-canvas text-ink-3 hover:text-ink-2"
                          }`}
                          title={s.enabled ? "点击停用" : "点击启用"}
                        >
                          {s.enabled ? "已启用" : "已停用"}
                        </button>
                        <button
                          onClick={() => setEditing(s)}
                          className="grid h-8 w-8 place-items-center rounded-control text-ink-3 transition hover:bg-canvas hover:text-ink"
                          title="编辑"
                          aria-label={`编辑 ${s.name}`}
                        >
                          <Pencil className="h-4 w-4" />
                        </button>
                        <button
                          onClick={() => void remove(s)}
                          disabled={busyId === s.id}
                          className="grid h-8 w-8 place-items-center rounded-control text-ink-3 transition hover:bg-danger-soft hover:text-danger disabled:opacity-50"
                          title="删除"
                          aria-label={`删除 ${s.name}`}
                        >
                          <Trash2 className="h-4 w-4" />
                        </button>
                      </div>
                    </div>

                    {open && (
                      <div className="border-t border-rule px-5 py-4">
                        {!p && (
                          <p className="text-small text-ink-3">
                            点击「测试连接」以拉取该服务器暴露的工具。
                          </p>
                        )}
                        {p && (
                          <>
                            {p.ok ? (
                              <p className="flex items-center gap-1.5 text-small text-verified">
                                <CheckCircle2 className="h-4 w-4" />
                                连接成功 · {p.latency_ms} ms · 暴露 {p.tool_count} 个工具
                              </p>
                            ) : (
                              <p className="flex items-start gap-1.5 break-all text-small text-danger">
                                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                                {p.error || "连接失败"}
                              </p>
                            )}
                            {p.ok && p.tools.length > 0 && (
                              <div className="mt-3 flex flex-col gap-1.5">
                                {p.tools.map((t) => (
                                  <div
                                    key={t.name}
                                    className="rounded-control border border-rule bg-canvas px-3 py-2"
                                  >
                                    <p className="flex items-center gap-1.5 font-mono text-small font-medium text-ink">
                                      <Wrench className="h-3.5 w-3.5 text-ink-3" />
                                      {t.name}
                                    </p>
                                    {t.description && (
                                      <p className="mt-0.5 text-small text-ink-3">{t.description}</p>
                                    )}
                                  </div>
                                ))}
                              </div>
                            )}
                          </>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      <McpServerModal
        target={editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          load();
        }}
      />
    </div>
  );
}

/** 添加/编辑 MCP server 弹窗。支持先「测试连接」再保存。 */
function McpServerModal({
  target,
  onClose,
  onSaved,
}: {
  target: MCPServer | "new" | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const isNew = target === "new";
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<MCPTransport>("stdio");
  const [command, setCommand] = useState("");
  const [argsText, setArgsText] = useState("");
  const [envText, setEnvText] = useState("");
  const [url, setUrl] = useState("");
  const [headersText, setHeadersText] = useState("");
  const [description, setDescription] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [testing, setTesting] = useState(false);
  const [probe, setProbe] = useState<MCPProbeResult | null>(null);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!target) return;
    if (target === "new") {
      setName("");
      setTransport("stdio");
      setCommand("");
      setArgsText("");
      setEnvText("");
      setUrl("");
      setHeadersText("");
      setDescription("");
      setEnabled(true);
    } else {
      setName(target.name);
      setTransport(target.transport);
      setCommand(target.command ?? "");
      setArgsText((target.args ?? []).join("\n"));
      setEnvText(Object.entries(target.env ?? {}).map(([k, v]) => `${k}=${v}`).join("\n"));
      setUrl(target.url ?? "");
      setHeadersText(
        Object.entries(target.headers ?? {}).map(([k, v]) => `${k}=${v}`).join("\n"),
      );
      setDescription(target.description ?? "");
      setEnabled(target.enabled);
    }
    setProbe(null);
    setErr(null);
  }, [target]);

  if (!target) return null;

  const form = (): MCPServerForm => {
    const base: MCPServerForm = { name: name.trim(), transport, enabled, description: description.trim() };
    if (transport === "stdio") {
      return {
        ...base,
        command: command.trim(),
        args: argsText.split("\n").map((l) => l.trim()).filter(Boolean),
        env: parseKv(envText),
      };
    }
    return { ...base, url: url.trim(), headers: parseKv(headersText) };
  };

  const runTest = async () => {
    if (!name.trim()) {
      setErr("请先填写名称");
      return;
    }
    setTesting(true);
    setErr(null);
    setProbe(null);
    try {
      setProbe(await testMcpServerAdHoc(form()));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setTesting(false);
    }
  };

  const submit = async () => {
    if (!name.trim()) {
      setErr("请填写名称");
      return;
    }
    setSaving(true);
    setErr(null);
    try {
      if (isNew) await createMcpServer(form());
      else await updateMcpServer((target as MCPServer).id, form());
      onSaved();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const labelCls = "block text-small font-medium text-ink-2";
  const inputCls =
    "mt-1.5 w-full rounded-control border border-rule bg-white px-3 py-2 text-small text-ink-2 outline-none transition placeholder:text-ink-3 focus:border-brand focus:ring-2 focus:ring-brand";

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={isNew ? "添加 MCP 服务器" : "编辑 MCP 服务器"}
        className="flex max-h-[88vh] w-full max-w-2xl flex-col rounded-panel border border-rule bg-white shadow-2xl"
      >
        <div className="flex items-center gap-2.5 border-b border-rule px-6 py-4">
          <ServerCog className="h-5 w-5 text-brand" />
          <h2 className="text-heading font-semibold text-ink">
            {isNew ? "添加 MCP 服务器" : "编辑 MCP 服务器"}
          </h2>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          <div className="grid grid-cols-2 gap-x-4">
            <div>
              <label htmlFor="mcp-name" className={labelCls}>
                名称 <span className="text-danger">*</span>
              </label>
              <input
                id="mcp-name"
                value={name}
                autoFocus
                onChange={(e) => setName(e.target.value)}
                placeholder="如：time"
                className={inputCls}
              />
            </div>
            <div>
              <label htmlFor="mcp-transport" className={labelCls}>
                传输方式
              </label>
              <select
                id="mcp-transport"
                value={transport}
                onChange={(e) => setTransport(e.target.value as MCPTransport)}
                className={inputCls}
              >
                <option value="stdio">stdio（本地子进程）</option>
                <option value="sse">SSE（远端）</option>
                <option value="http">HTTP（远端）</option>
              </select>
            </div>
          </div>

          {transport === "stdio" ? (
            <>
              <div className="mt-3.5">
                <label htmlFor="mcp-command" className={labelCls}>
                  启动命令 <span className="text-danger">*</span>
                </label>
                <input
                  id="mcp-command"
                  value={command}
                  onChange={(e) => setCommand(e.target.value)}
                  placeholder="如：cmd（Windows 推荐）或 npx"
                  className={inputCls}
                />
              </div>
              <div className="mt-3.5">
                <label htmlFor="mcp-args" className={labelCls}>
                  参数（每行一个）
                </label>
                <textarea
                  id="mcp-args"
                  value={argsText}
                  onChange={(e) => setArgsText(e.target.value)}
                  rows={4}
                  placeholder={"/c\nnpx\n-y\n@modelcontextprotocol/server-time"}
                  className={`${inputCls} font-mono`}
                />
              </div>
              <div className="mt-3.5">
                <label htmlFor="mcp-env" className={labelCls}>
                  环境变量（每行 KEY=VALUE）
                </label>
                <textarea
                  id="mcp-env"
                  value={envText}
                  onChange={(e) => setEnvText(e.target.value)}
                  rows={3}
                  placeholder={"API_KEY=xxx"}
                  className={`${inputCls} font-mono`}
                />
              </div>
            </>
          ) : (
            <>
              <div className="mt-3.5">
                <label htmlFor="mcp-url" className={labelCls}>
                  URL <span className="text-danger">*</span>
                </label>
                <input
                  id="mcp-url"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="如：https://example.com/sse"
                  className={inputCls}
                />
              </div>
              <div className="mt-3.5">
                <label htmlFor="mcp-headers" className={labelCls}>
                  请求头（每行 KEY=VALUE）
                </label>
                <textarea
                  id="mcp-headers"
                  value={headersText}
                  onChange={(e) => setHeadersText(e.target.value)}
                  rows={3}
                  placeholder={"Authorization=Bearer xxx"}
                  className={`${inputCls} font-mono`}
                />
              </div>
            </>
          )}

          <div className="mt-3.5">
            <label htmlFor="mcp-desc" className={labelCls}>
              备注（可选）
            </label>
            <input
              id="mcp-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className={inputCls}
            />
          </div>

          <label className="mt-4 flex cursor-pointer items-center gap-2 text-small text-ink-2">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
              className="h-4 w-4 accent-[var(--brand)]"
            />
            启用该服务器
          </label>

          {probe && (
            <div className="mt-3.5 rounded-panel border border-rule bg-canvas px-4 py-3">
              {probe.ok ? (
                <p className="flex items-center gap-1.5 text-small text-verified">
                  <CheckCircle2 className="h-4 w-4" />
                  连接成功 · {probe.latency_ms} ms · 暴露 {probe.tool_count} 个工具
                </p>
              ) : (
                <p className="flex items-start gap-1.5 break-all text-small text-danger">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  {probe.error || "连接失败"}
                </p>
              )}
              {probe.ok && probe.tools.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {probe.tools.map((t) => (
                    <span
                      key={t.name}
                      className="rounded-control border border-rule bg-white px-2 py-0.5 font-mono text-small text-ink-2"
                    >
                      {t.name}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}

          {err && (
            <p className="mt-3.5 flex items-start gap-1.5 break-all text-small text-danger">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" /> {err}
            </p>
          )}
        </div>

        <div className="flex items-center gap-2 border-t border-rule bg-canvas px-6 py-3.5">
          <button
            onClick={() => void runTest()}
            disabled={testing || saving}
            className="inline-flex items-center gap-1.5 rounded-control border border-rule bg-white px-3.5 py-2 text-small font-medium text-ink-2 transition hover:border-brand hover:text-brand disabled:opacity-50"
          >
            {testing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plug className="h-4 w-4" />}
            测试连接
          </button>
          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={onClose}
              className="rounded-control px-3.5 py-2 text-small text-ink-3 transition hover:text-ink-2"
            >
              取消
            </button>
            <button
              onClick={() => void submit()}
              disabled={saving || testing}
              className="inline-flex items-center gap-1.5 rounded-control bg-brand px-4 py-2 text-small font-medium text-white transition hover:bg-brand/90 disabled:opacity-50"
            >
              {saving && <Loader2 className="h-4 w-4 animate-spin" />}
              保存
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/** "KEY=VALUE" 多行文本 → 对象（空行/无等号行忽略）。 */
function parseKv(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const t = line.trim();
    if (!t || !t.includes("=")) continue;
    const i = t.indexOf("=");
    out[t.slice(0, i).trim()] = t.slice(i + 1).trim();
  }
  return out;
}
