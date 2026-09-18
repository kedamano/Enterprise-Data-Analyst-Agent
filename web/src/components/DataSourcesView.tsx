import { useEffect, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Database,
  Loader2,
  Lock,
  Plug,
  Plus,
  RefreshCw,
  ServerCog,
  Trash2,
} from "@/components/icons";
import {
  createDataSource,
  deleteDataSource,
  fetchDataSources,
  fetchHealth,
  testDataSource,
  type DataSourceConn,
  type DataSourceForm,
  type HealthInfo,
} from "@/lib/api";

const DIALECT_META: Record<string, { label: string; cls: string }> = {
  sqlite: { label: "SQLite", cls: "bg-canvas text-ink-2" },
  postgresql: { label: "PostgreSQL", cls: "bg-brand-soft text-brand" },
  postgres: { label: "PostgreSQL", cls: "bg-brand-soft text-brand" },
  mysql: { label: "MySQL", cls: "bg-attention-soft text-attention" },
  unknown: { label: "未知类型", cls: "bg-canvas text-ink-3" },
};

type Dialect = DataSourceForm["dialect"];

const DEFAULT_PORTS: Record<Exclude<Dialect, "sqlite">, number> = {
  mysql: 3306,
  postgresql: 5432,
};

/**
 * 数据源页：只保留关键信息——连接名 / 类型 / 只读 / 来源。
 * DSN 串（含账号密码位）、主机端口拆解、.env 配置教程等一律不上页面：
 * 它们是运维视角的细节，业务用户只需要知道「有哪些库可用、能不能删」。
 * 新增连接走「新建连接」弹窗（Navicat 式表单），不再要求改 .env 重启。
 */
export function DataSourcesView() {
  const [conns, setConns] = useState<DataSourceConn[] | null>(null);
  const [info, setInfo] = useState<HealthInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const load = () => {
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    Promise.all([
      fetchDataSources(ctrl.signal).then((r) => setConns(r.sources)),
      fetchHealth(ctrl.signal).then(setInfo),
    ])
      .catch((e) => {
        if ((e as DOMException)?.name === "AbortError") return;
        setError((e as Error).message);
      })
      .finally(() => setLoading(false));
    return () => ctrl.abort();
  };

  useEffect(load, []);

  // 用户看得懂的三种模型状态，而不是把后端枚举值（real / mock）直接摆出来。
  const llmLabel = info?.llm_degraded
    ? "模型：降级中"
    : info?.mock_llm
      ? "模型：模拟输出"
      : "模型：正常";
  const llmOk = Boolean(info) && !info?.llm_degraded && !info?.mock_llm;

  const removeConn = async (name: string) => {
    if (!window.confirm(`确定删除连接「${name}」吗？删除后智能体将无法再访问它。`)) return;
    setDeleting(name);
    setActionError(null);
    try {
      await deleteDataSource(name);
      setConns((prev) => prev?.filter((c) => c.name !== name) ?? null);
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setDeleting(null);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col bg-canvas">
      {/* 页头 */}
      <div className="flex h-14 shrink-0 items-center gap-2.5 border-b border-rule bg-white px-6">
        <Database className="h-5 w-5 text-verified" />
        <h1 className="text-heading font-semibold text-ink">数据源</h1>
        <span className="text-small text-ink-3">
          数据库连接 · 只读，不会产生写入
        </span>
        <div className="ml-auto flex items-center gap-2">
          {info && (
            <span
              className={`rounded-full border px-3 py-1 text-small ${
                llmOk
                  ? "border-verified bg-verified-soft text-verified"
                  : "border-attention bg-attention-soft text-attention"
              }`}
            >
              {llmLabel}
            </span>
          )}
          <button
            onClick={() => setCreating(true)}
            className="inline-flex items-center gap-1.5 rounded-control bg-brand px-3.5 py-2 text-small font-medium text-white transition hover:bg-brand/90"
          >
            <Plus className="h-4 w-4" /> 新建连接
          </button>
          <button
            onClick={() => void load()}
            className="grid h-9 w-9 place-items-center rounded-control border border-rule text-ink-3 transition hover:bg-canvas hover:text-ink"
            title="刷新"
            aria-label="刷新数据源"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[1200px] px-6 py-6">
          {error && (
            <div className="mb-5 flex items-center gap-2.5 rounded-panel border border-danger bg-danger-soft px-4 py-3 text-small text-danger">
              <AlertCircle className="h-4 w-4 shrink-0" /> {error}
            </div>
          )}
          {actionError && (
            <div className="mb-5 flex items-center gap-2.5 rounded-panel border border-danger bg-danger-soft px-4 py-3 text-small text-danger">
              <AlertCircle className="h-4 w-4 shrink-0" /> {actionError}
            </div>
          )}

          {/* 连接清单：只留名字 / 类型 / 只读 / 来源 */}
          <div className="mb-3 flex items-center gap-2.5">
            <ServerCog className="h-4 w-4 text-ink-3" />
            <h2 className="text-body font-semibold text-ink">
              数据库连接（{conns?.length ?? 0}）
            </h2>
          </div>

          {(conns ?? []).length === 0 && !loading ? (
            <div className="rounded-panel border border-dashed border-rule-strong bg-white px-6 py-12 text-center">
              <Plug className="mx-auto h-9 w-9 text-ink-3" />
              <p className="mt-3 text-body font-medium text-ink-2">还没有数据源</p>
              <p className="mt-1.5 text-small text-ink-3">
                点击右上角「新建连接」接入业务数据库（支持 MySQL / PostgreSQL / SQLite），
                <br />
                连接成功后即可直接向智能体提问。
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3.5 sm:grid-cols-2 lg:grid-cols-3">
              {(conns ?? []).map((c, i) => {
                const dm = DIALECT_META[c.dialect] ?? DIALECT_META.unknown;
                return (
                  <div
                    key={c.name}
                    className="flex flex-col rounded-panel border border-rule bg-white p-5"
                  >
                    <div className="flex items-start gap-3">
                      <span className="grid h-10 w-10 shrink-0 place-items-center rounded-panel bg-verified-soft text-verified ring-1 ring-verified">
                        <Database className="h-5 w-5" />
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-body font-semibold text-ink" title={c.name}>
                          {c.name}
                        </p>
                        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                          <span
                            className={`rounded-control px-2 py-0.5 text-small font-medium ${dm.cls}`}
                          >
                            {dm.label}
                          </span>
                          {i === 0 && (
                            <span className="rounded-control bg-brand-soft px-2 py-0.5 text-small font-medium text-brand">
                              主源
                            </span>
                          )}
                          {c.readonly && (
                            <span className="inline-flex items-center gap-1 rounded-control bg-canvas px-2 py-0.5 text-small text-ink-3">
                              <Lock className="h-3.5 w-3.5" /> 只读
                            </span>
                          )}
                        </div>
                      </div>
                      {c.origin === "local" && (
                        <button
                          onClick={() => void removeConn(c.name)}
                          disabled={deleting === c.name}
                          className="grid h-8 w-8 shrink-0 place-items-center rounded-control text-ink-3 transition hover:bg-danger-soft hover:text-danger disabled:opacity-50"
                          title="删除连接"
                          aria-label={`删除连接 ${c.name}`}
                        >
                          {deleting === c.name ? (
                            <Loader2 className="h-4 w-4 animate-spin" />
                          ) : (
                            <Trash2 className="h-4 w-4" />
                          )}
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      <NewConnectionModal
        open={creating}
        existingNames={(conns ?? []).map((c) => c.name)}
        onClose={() => setCreating(false)}
        onSaved={() => {
          setCreating(false);
          load();
        }}
      />
    </div>
  );
}

/** Navicat 式「新建连接」弹窗：填表 → 测试连接 → 保存。 */
function NewConnectionModal({
  open,
  existingNames,
  onClose,
  onSaved,
}: {
  open: boolean;
  existingNames: string[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState("");
  const [dialect, setDialect] = useState<Dialect>("mysql");
  const [host, setHost] = useState("127.0.0.1");
  const [port, setPort] = useState<string>("3306");
  const [database, setDatabase] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [path, setPath] = useState("");
  const [testing, setTesting] = useState(false);
  const [testOk, setTestOk] = useState(false);
  const [testError, setTestError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const reset = () => {
    setName("");
    setDialect("mysql");
    setHost("127.0.0.1");
    setPort("3306");
    setDatabase("");
    setUsername("");
    setPassword("");
    setPath("");
    setTestOk(false);
    setTestError(null);
    setSaveError(null);
  };

  useEffect(() => {
    if (open) reset();
  }, [open]);

  const switchDialect = (d: Dialect) => {
    setDialect(d);
    setTestOk(false);
    setTestError(null);
    if (d !== "sqlite") setPort(String(DEFAULT_PORTS[d]));
  };

  const form = (): DataSourceForm =>
    dialect === "sqlite"
      ? { dialect, path: path.trim() }
      : {
          dialect,
          host: host.trim(),
          port: Number(port) || DEFAULT_PORTS[dialect],
          database: database.trim(),
          username: username.trim(),
          password,
        };

  const runTest = async () => {
    setTesting(true);
    setTestError(null);
    setTestOk(false);
    try {
      const r = await testDataSource(form());
      if (r.ok) setTestOk(true);
      else setTestError(r.error || "连接失败");
    } catch (e) {
      setTestError((e as Error).message);
    } finally {
      setTesting(false);
    }
  };

  const submit = async () => {
    const n = name.trim();
    if (!n) {
      setSaveError("请填写连接名称");
      return;
    }
    if (n.toLowerCase() === "default") {
      setSaveError('"default" 是主数据源保留名，请换一个名称');
      return;
    }
    if (existingNames.includes(n)) {
      setSaveError(`连接名「${n}」已存在`);
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      await createDataSource({ ...form(), name: n });
      onSaved();
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  if (!open) return null;

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
        aria-label="新建连接"
        className="w-full max-w-md rounded-panel border border-rule bg-white shadow-2xl"
      >
        <div className="flex items-center gap-2.5 border-b border-rule px-6 py-4">
          <Plug className="h-5 w-5 text-brand" />
          <h2 className="text-heading font-semibold text-ink">新建连接</h2>
        </div>

        <div className="px-6 py-5">
          <div className="grid grid-cols-2 gap-x-4">
            <div>
              <label htmlFor="ds-name" className={labelCls}>
                连接名称 <span className="text-danger">*</span>
              </label>
              <input
                id="ds-name"
                value={name}
                autoFocus
                onChange={(e) => setName(e.target.value)}
                placeholder="如：业务库-订单"
                className={inputCls}
              />
            </div>
            <div>
              <label htmlFor="ds-dialect" className={labelCls}>
                数据库类型
              </label>
              <select
                id="ds-dialect"
                value={dialect}
                onChange={(e) => switchDialect(e.target.value as Dialect)}
                className={inputCls}
              >
                <option value="mysql">MySQL</option>
                <option value="postgresql">PostgreSQL</option>
                <option value="sqlite">SQLite</option>
              </select>
            </div>
          </div>

          {dialect === "sqlite" ? (
            <div className="mt-3.5">
              <label htmlFor="ds-path" className={labelCls}>
                数据库文件路径 <span className="text-danger">*</span>
              </label>
              <input
                id="ds-path"
                value={path}
                onChange={(e) => setPath(e.target.value)}
                placeholder="如：D:\data\biz.db 或 ./data/biz.db"
                className={inputCls}
              />
            </div>
          ) : (
            <>
              <div className="mt-3.5 grid grid-cols-3 gap-x-4">
                <div className="col-span-2">
                  <label htmlFor="ds-host" className={labelCls}>
                    主机 <span className="text-danger">*</span>
                  </label>
                  <input
                    id="ds-host"
                    value={host}
                    onChange={(e) => setHost(e.target.value)}
                    placeholder="localhost"
                    className={inputCls}
                  />
                </div>
                <div>
                  <label htmlFor="ds-port" className={labelCls}>
                    端口
                  </label>
                  <input
                    id="ds-port"
                    value={port}
                    onChange={(e) => setPort(e.target.value.replace(/\D/g, ""))}
                    placeholder={String(DEFAULT_PORTS[dialect])}
                    inputMode="numeric"
                    className={inputCls}
                  />
                </div>
              </div>
              <div className="mt-3.5">
                <label htmlFor="ds-db" className={labelCls}>
                  数据库名 <span className="text-danger">*</span>
                </label>
                <input
                  id="ds-db"
                  value={database}
                  onChange={(e) => setDatabase(e.target.value)}
                  placeholder="如：erp_orders"
                  className={inputCls}
                />
              </div>
              <div className="mt-3.5 grid grid-cols-2 gap-x-4">
                <div>
                  <label htmlFor="ds-user" className={labelCls}>
                    用户名
                  </label>
                  <input
                    id="ds-user"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    autoComplete="off"
                    className={inputCls}
                  />
                </div>
                <div>
                  <label htmlFor="ds-pass" className={labelCls}>
                    密码
                  </label>
                  <input
                    id="ds-pass"
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    autoComplete="new-password"
                    className={inputCls}
                  />
                </div>
              </div>
            </>
          )}

          {/* 测试结果 / 保存错误 */}
          {testOk && (
            <p className="mt-3.5 flex items-center gap-1.5 text-small text-verified">
              <CheckCircle2 className="h-4 w-4" /> 连接成功，可以保存了
            </p>
          )}
          {testError && (
            <p className="mt-3.5 flex items-start gap-1.5 break-all text-small text-danger">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" /> {testError}
            </p>
          )}
          {saveError && (
            <p className="mt-3.5 flex items-start gap-1.5 break-all text-small text-danger">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" /> {saveError}
            </p>
          )}

          <p className="mt-3.5 text-small leading-relaxed text-ink-3">
            保存前会自动测试连接；接入后智能体只能<b>只读查询</b>，不会写入数据。
          </p>
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
              确定
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
