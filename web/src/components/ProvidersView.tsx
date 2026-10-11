import { useCallback, useEffect, useState } from "react";
import {
  fetchProviders,
  fetchActiveProvider,
  createProvider,
  updateProvider,
  deleteProvider,
  probeProvider,
  testProvider,
  activateProvider,
  type Provider,
} from "@/lib/api";
import {
  Cpu,
  RefreshCw,
  Plus,
  Trash2,
  Check,
  Loader2,
  Zap,
  ShieldCheck,
  ServerCog,
} from "@/components/icons";

interface FormState {
  name: string;
  base_url: string;
  api_key: string;
}

const EMPTY_FORM: FormState = { name: "", base_url: "", api_key: "" };

export function ProvidersView() {
  const [providers, setProviders] = useState<Provider[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [live, setLive] = useState<{ base_url: string; model: string; is_mock: boolean } | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [editing, setEditing] = useState<number | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [loading, setLoading] = useState(false);
  const [probing, setProbing] = useState<Record<number, boolean>>({});
  const [testing, setTesting] = useState<Record<number, boolean>>({});
  const [activating, setActivating] = useState<Record<number, boolean>>({});
  const [testResults, setTestResults] = useState<Record<number, { ok: boolean; detail: string }>>({});
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [{ providers: list }, active] = await Promise.all([
        fetchProviders(),
        fetchActiveProvider(),
      ]);
      setProviders(list);
      setActiveId(active.provider?.id ?? null);
      setLive(active.live ?? null);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleSave = useCallback(async () => {
    if (!form.name.trim() || !form.base_url.trim()) {
      setError("Name and Base URL are required");
      return;
    }
    setLoading(true);
    try {
      if (editing) {
        await updateProvider(editing, form);
      } else {
        await createProvider(form);
      }
      setForm(EMPTY_FORM);
      setEditing(null);
      setShowForm(false);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [form, editing, refresh]);

  const handleEdit = useCallback((p: Provider) => {
    setForm({ name: p.name, base_url: p.base_url, api_key: p.api_key });
    setEditing(p.id);
    setShowForm(true);
  }, []);

  const handleDelete = useCallback(
    async (id: number) => {
      if (!confirm("Delete this provider?")) return;
      await deleteProvider(id);
      await refresh();
    },
    [refresh],
  );

  const handleProbe = useCallback(
    async (id: number) => {
      setProbing((m) => ({ ...m, [id]: true }));
      try {
        const r = await probeProvider(id);
        await refresh();
        setTestResults((m) => ({
          ...m,
          [id]: { ok: r.ok, detail: r.ok ? `Found ${r.count} models` : "No models found" },
        }));
      } catch (e) {
        setTestResults((m) => ({ ...m, [id]: { ok: false, detail: (e as Error).message } }));
      } finally {
        setProbing((m) => ({ ...m, [id]: false }));
      }
    },
    [refresh],
  );

  const handleTest = useCallback(
    async (id: number) => {
      setTesting((m) => ({ ...m, [id]: true }));
      try {
        const r = await testProvider(id);
        setTestResults((m) => ({ ...m, [id]: { ok: r.ok, detail: r.detail } }));
      } catch (e) {
        setTestResults((m) => ({ ...m, [id]: { ok: false, detail: (e as Error).message } }));
      } finally {
        setTesting((m) => ({ ...m, [id]: false }));
      }
    },
    [],
  );

  const handleActivate = useCallback(
    async (id: number, model?: string) => {
      setActivating((m) => ({ ...m, [id]: true }));
      try {
        await activateProvider(id, model);
        await refresh();
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setActivating((m) => ({ ...m, [id]: false }));
      }
    },
    [refresh],
  );

  return (
    <div className="flex h-full overflow-hidden">
      {/* Left panel: provider list */}
      <aside className="flex w-80 shrink-0 flex-col border-r border-rule bg-canvas">
        <div className="flex items-center justify-between border-b border-rule px-4 py-3">
          <div className="flex items-center gap-2">
            <ServerCog className="h-5 w-5 text-brand" />
            <h1 className="text-body font-semibold text-ink">Model Providers</h1>
          </div>
          <button
            onClick={() => {
              setForm(EMPTY_FORM);
              setEditing(null);
              setShowForm(true);
            }}
            className="inline-flex items-center gap-1.5 rounded-control border border-brand bg-brand px-2.5 py-1.5 text-micro font-medium text-white shadow-sm transition hover:brightness-110"
            aria-label="Add provider"
            title="Add provider"
          >
            <Plus className="h-3.5 w-3.5" />
            Add
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-3 space-y-2">
          {providers.length === 0 ? (
            <div className="rounded-control border border-dashed border-rule p-6 text-center text-small text-ink-3">
              No providers yet. Add one to get started.
            </div>
          ) : (
            providers.map((p) => {
              const isActive = p.id === activeId;
              const probeLoading = probing[p.id];
              const testLoading = testing[p.id];
              const activateLoading = activating[p.id];
              const result = testResults[p.id];
              return (
                <div
                  key={p.id}
                  className={`rounded-control border p-3 transition ${
                    isActive
                      ? "border-brand bg-brand-soft ring-1 ring-brand"
                      : "border-rule bg-white hover:border-rule-strong"
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        {isActive && (
                          <Check className="h-4 w-4 text-brand" />
                        )}
                        <span className="text-body font-medium text-ink truncate">{p.name}</span>
                      </div>
                      <p className="mt-0.5 truncate text-micro text-ink-3 font-mono">{p.base_url}</p>
                      <p className="text-micro text-ink-3">
                        {p.available_models.length > 0
                          ? `${p.available_models.length} models`
                          : "No models discovered"}
                      </p>
                    </div>
                  </div>

                  {/* Model list */}
                  {p.available_models.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1">
                      {p.available_models.map((m) => (
                        <button
                          key={m}
                          onClick={() => handleActivate(p.id, m)}
                          disabled={activateLoading}
                          className={`rounded px-1.5 py-0.5 text-micro transition ${
                            live?.model === m && isActive
                              ? "bg-brand text-white"
                              : "bg-canvas text-ink-2 hover:bg-rule"
                          }`}
                          title={`Activate with model: ${m}`}
                        >
                          {m}
                        </button>
                      ))}
                    </div>
                  )}

                  {/* Test result */}
                  {result && (
                    <div
                      className={`mt-2 rounded p-2 text-micro ${
                        result.ok
                          ? "bg-verified-soft text-verified border border-verified/30"
                          : "bg-destructive/5 text-destructive border border-destructive/20"
                      }`}
                    >
                      {result.ok ? "OK: " : "Failed: "}
                      {result.detail}
                    </div>
                  )}

                  {/* Actions */}
                  <div className="mt-2 flex items-center gap-1.5">
                    <button
                      onClick={() => handleProbe(p.id)}
                      disabled={probeLoading}
                      className="inline-flex items-center gap-1 rounded px-2 py-1 text-micro text-ink-2 transition hover:bg-canvas disabled:opacity-50"
                      title="Discover models"
                    >
                      {probeLoading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
                      Refresh
                    </button>
                    <button
                      onClick={() => handleTest(p.id)}
                      disabled={testLoading}
                      className="inline-flex items-center gap-1 rounded px-2 py-1 text-micro text-ink-2 transition hover:bg-canvas disabled:opacity-50"
                      title="Test connectivity"
                    >
                      {testLoading ? <Loader2 className="h-3 w-3 animate-spin" /> : <Zap className="h-3 w-3" />}
                      Test
                    </button>
                    <button
                      onClick={() => handleActivate(p.id)}
                      disabled={activateLoading}
                      className="inline-flex items-center gap-1 rounded px-2 py-1 text-micro text-ink-2 transition hover:bg-canvas disabled:opacity-50"
                      title="Activate this provider"
                    >
                      {activateLoading ? <Loader2 className="h-3 w-3 animate-spin" /> : <Zap className="h-3 w-3" />}
                      Activate
                    </button>
                    <button
                      onClick={() => handleEdit(p)}
                      className="inline-flex items-center gap-1 rounded px-2 py-1 text-micro text-ink-2 transition hover:bg-canvas"
                      title="Edit"
                    >
                      Edit
                    </button>
                    <button
                      onClick={() => handleDelete(p.id)}
                      className="ml-auto inline-flex items-center gap-1 rounded px-2 py-1 text-micro text-destructive transition hover:bg-destructive/5"
                      title="Delete"
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </aside>

      {/* Right panel: detail / form */}
      <main className="flex-1 overflow-y-auto bg-white">
        {error && (
          <div className="mx-4 mt-4 rounded-control border border-destructive/30 bg-destructive/5 p-3 text-small text-destructive">
            {error}
          </div>
        )}

        {/* Active config banner */}
        {live && (
          <div className="mx-4 mt-4 flex items-center justify-between rounded-control border border-brand/30 bg-brand-soft p-3">
            <div className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-brand" />
              <span className="text-body text-ink">
                Active: <strong>{live.model}</strong>
                {live.is_mock && <span className="ml-2 text-attention">(Mock Mode)</span>}
              </span>
            </div>
            <span className="font-mono text-micro text-ink-3">{live.base_url}</span>
          </div>
        )}

        {/* Form */}
        {showForm ? (
          <div className="mx-4 mt-4 rounded-control border border-rule bg-canvas p-4">
            <h2 className="text-body font-semibold text-ink">
              {editing ? "Edit Provider" : "New Provider"}
            </h2>
            <div className="mt-4 space-y-3">
              <div>
                <label className="block text-micro font-medium text-ink-2 mb-1">Name</label>
                <input
                  value={form.name}
                  onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                  placeholder="My Provider"
                  className="w-full rounded-control border border-rule px-3 py-2 text-body text-ink transition focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand"
                />
              </div>
              <div>
                <label className="block text-micro font-medium text-ink-2 mb-1">Base URL</label>
                <input
                  value={form.base_url}
                  onChange={(e) => setForm((f) => ({ ...f, base_url: e.target.value }))}
                  placeholder="https://api.openai.com/v1"
                  className="w-full rounded-control border border-rule px-3 py-2 text-body text-ink transition focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand font-mono text-small"
                />
              </div>
              <div>
                <label className="block text-micro font-medium text-ink-2 mb-1">API Key</label>
                <input
                  type="password"
                  value={form.api_key}
                  onChange={(e) => setForm((f) => ({ ...f, api_key: e.target.value }))}
                  placeholder="sk-..."
                  className="w-full rounded-control border border-rule px-3 py-2 text-body text-ink transition focus:border-brand focus:outline-none focus:ring-1 focus:ring-brand font-mono text-small"
                />
              </div>
            </div>
            <div className="mt-4 flex items-center gap-2">
              <button
                onClick={handleSave}
                disabled={loading}
                className="inline-flex items-center gap-1.5 rounded-control border border-brand bg-brand px-3 py-1.5 text-small font-medium text-white transition hover:brightness-110 disabled:opacity-50"
              >
                {loading && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                Save
              </button>
              <button
                onClick={() => {
                  setShowForm(false);
                  setEditing(null);
                  setForm(EMPTY_FORM);
                }}
                className="rounded-control border border-rule px-3 py-1.5 text-small text-ink-2 transition hover:bg-canvas"
              >
                Cancel
              </button>
              {!editing && (
                <button
                  onClick={async () => {
                    await handleSave();
                    // After save, probe
                    if (!editing) return;
                    await handleProbe(editing);
                  }}
                  disabled={loading}
                  className="ml-auto inline-flex items-center gap-1.5 rounded-control border border-rule px-3 py-1.5 text-small text-ink-2 transition hover:bg-canvas disabled:opacity-50"
                >
                  Save & Discover Models
                </button>
              )}
            </div>
          </div>
        ) : (
          <div className="mx-4 mt-4 rounded-control border border-dashed border-rule p-8 text-center">
            <Cpu className="mx-auto h-8 w-8 text-ink-3" />
            <p className="mt-3 text-body text-ink-2">
              {providers.length > 0
                ? "Select a provider to view details, or add a new one."
                : "Add your first model provider to get started."}
            </p>
            <button
              onClick={() => setShowForm(true)}
              className="mt-4 inline-flex items-center gap-1.5 rounded-control border border-brand bg-brand px-4 py-2 text-body font-medium text-white transition hover:brightness-110"
            >
              <Plus className="h-4 w-4" /> Add Provider
            </button>
          </div>
        )}

        {/* Help */}
        <div className="mx-4 mt-6 mb-4 rounded-control border border-rule bg-canvas p-4">
          <h3 className="text-body font-semibold text-ink">How to use</h3>
          <ul className="mt-2 space-y-1.5 text-small text-ink-2">
            <li className="flex gap-2">
              <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-brand" />
              Add a provider with its name, base URL, and API key
            </li>
            <li className="flex gap-2">
              <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-brand" />
              Click <strong>Refresh</strong> to auto-discover available models
            </li>
            <li className="flex gap-2">
              <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-brand" />
              Click <strong>Test</strong> to verify end-to-end connectivity
            </li>
            <li className="flex gap-2">
              <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-brand" />
              Click a model name or <strong>Activate</strong> to switch live
            </li>
          </ul>
        </div>
      </main>
    </div>
  );
}
