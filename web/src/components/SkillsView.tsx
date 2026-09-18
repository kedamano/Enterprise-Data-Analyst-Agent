import { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  FileUp,
  Loader2,
  Package,
  Pencil,
  Plus,
  RefreshCw,
  Sparkles,
  Trash2,
} from "@/components/icons";
import {
  createSkill,
  deleteSkill,
  fetchSkill,
  fetchSkills,
  importSkillsZip,
  setSkillEnabled,
  updateSkill,
  type Skill,
} from "@/lib/api";

/**
 * 技能页：管理"给大模型的分析方法论/口径/领域知识"。
 *
 * 一个技能 = 一个 `SKILL.md`（YAML frontmatter: name/description + Markdown 正文），
 * 兼容 Anthropic Skills 目录形态，可 zip 导入。
 * 技能本身**不影响任何对话**——只有在对话里勾选了，才会把正文注入本轮提示词。
 */
export function SkillsView() {
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<Skill | "new" | null>(null);
  const [importing, setImporting] = useState(false);
  const [importMsg, setImportMsg] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = () => {
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    fetchSkills(ctrl.signal)
      .then((r) => setSkills(r.skills))
      .catch((e) => {
        if ((e as DOMException)?.name === "AbortError") return;
        setError((e as Error).message);
      })
      .finally(() => setLoading(false));
    return () => ctrl.abort();
  };

  useEffect(load, []);

  const toggle = async (s: Skill) => {
    setBusyId(s.id);
    setError(null);
    try {
      const updated = await setSkillEnabled(s.id, !s.enabled);
      setSkills((prev) =>
        (prev ?? []).map((x) => (x.id === s.id ? { ...x, ...updated } : x)),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (s: Skill) => {
    if (!window.confirm(`确定删除技能「${s.name}」吗？此操作不可撤销。`)) return;
    setBusyId(s.id);
    setError(null);
    try {
      await deleteSkill(s.id);
      setSkills((prev) => (prev ?? []).filter((x) => x.id !== s.id));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusyId(null);
    }
  };

  const onPickZip = async (file: File | undefined) => {
    if (!file) return;
    setImporting(true);
    setImportMsg(null);
    setError(null);
    try {
      const r = await importSkillsZip(file);
      const n = r.imported?.length ?? 0;
      setImportMsg(
        r.errors?.length
          ? `导入 ${n} 个，${r.errors.length} 个失败：${r.errors.join("；")}`
          : `导入成功 ${n} 个技能`,
      );
      load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setImporting(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col bg-canvas">
      <div className="flex h-14 shrink-0 items-center gap-2.5 border-b border-rule bg-white px-6">
        <Sparkles className="h-5 w-5 text-brand" />
        <h1 className="text-heading font-semibold text-ink">技能</h1>
        <span className="text-small text-ink-3">
          分析方法论 / 口径 / 领域知识 · 对话中勾选后注入大模型
        </span>
        <div className="ml-auto flex items-center gap-2">
          <input
            ref={fileRef}
            type="file"
            accept=".zip,application/zip"
            className="hidden"
            onChange={(e) => void onPickZip(e.target.files?.[0])}
          />
          <button
            onClick={() => fileRef.current?.click()}
            disabled={importing}
            className="inline-flex items-center gap-1.5 rounded-control border border-rule bg-white px-3.5 py-2 text-small font-medium text-ink-2 transition hover:border-brand hover:text-brand disabled:opacity-50"
          >
            {importing ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileUp className="h-4 w-4" />}
            导入 zip
          </button>
          <button
            onClick={() => setEditing("new")}
            className="inline-flex items-center gap-1.5 rounded-control bg-brand px-3.5 py-2 text-small font-medium text-white transition hover:bg-brand/90"
          >
            <Plus className="h-4 w-4" /> 新建技能
          </button>
          <button
            onClick={() => void load()}
            className="grid h-9 w-9 place-items-center rounded-control border border-rule text-ink-3 transition hover:bg-canvas hover:text-ink"
            title="刷新"
            aria-label="刷新技能列表"
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
          {importMsg && (
            <div className="mb-5 flex items-center gap-2.5 rounded-panel border border-rule bg-white px-4 py-3 text-small text-ink-2">
              <FileUp className="h-4 w-4 shrink-0 text-ink-3" /> {importMsg}
            </div>
          )}

          <div className="mb-3 flex items-center gap-2.5">
            <Package className="h-4 w-4 text-ink-3" />
            <h2 className="text-body font-semibold text-ink">
              已安装技能（{skills?.length ?? 0}）
            </h2>
          </div>

          {(skills ?? []).length === 0 && !loading ? (
            <div className="rounded-panel border border-dashed border-rule-strong bg-white px-6 py-12 text-center">
              <Sparkles className="mx-auto h-9 w-9 text-ink-3" />
              <p className="mt-3 text-body font-medium text-ink-2">还没有技能</p>
              <p className="mt-1.5 text-small text-ink-3">
                点击「新建技能」手写一条分析方法论，或「导入 zip」直接引入
                <br />
                现成的 <code>SKILL.md</code> 技能包（兼容 Anthropic Skills 约定）。
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3.5 sm:grid-cols-2 lg:grid-cols-3">
              {(skills ?? []).map((s) => (
                <div
                  key={s.id}
                  className={`flex flex-col rounded-panel border bg-white p-5 ${
                    s.enabled ? "border-rule" : "border-rule opacity-60"
                  }`}
                >
                  <div className="flex items-start gap-3">
                    <span className="grid h-10 w-10 shrink-0 place-items-center rounded-panel bg-brand-soft text-brand ring-1 ring-brand">
                      <Sparkles className="h-5 w-5" />
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-body font-semibold text-ink" title={s.name}>
                        {s.name}
                      </p>
                      <p className="mt-1 line-clamp-2 text-small text-ink-3">
                        {s.description || "（无描述）"}
                      </p>
                    </div>
                  </div>

                  <div className="mt-3 flex flex-wrap items-center gap-1.5">
                    <span className="rounded-control bg-canvas px-2 py-0.5 text-small text-ink-3">
                      {s.origin === "imported" ? "导入" : "手写"}
                    </span>
                    <span className="text-small text-ink-3">{s.chars} 字</span>
                    {!s.enabled && (
                      <span className="rounded-control bg-canvas px-2 py-0.5 text-small text-ink-3">
                        已停用
                      </span>
                    )}
                  </div>

                  <div className="mt-4 flex items-center gap-2 border-t border-rule pt-3">
                    <button
                      onClick={() => void toggle(s)}
                      disabled={busyId === s.id}
                      className={`inline-flex items-center gap-1.5 rounded-control px-2.5 py-1.5 text-small font-medium transition disabled:opacity-50 ${
                        s.enabled
                          ? "bg-verified-soft text-verified"
                          : "bg-canvas text-ink-3 hover:text-ink-2"
                      }`}
                      title={s.enabled ? "点击停用（不出现在对话勾选项）" : "点击启用"}
                    >
                      {busyId === s.id ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : null}
                      {s.enabled ? "已启用" : "已停用"}
                    </button>
                    <div className="ml-auto flex items-center gap-1">
                      <button
                        onClick={() => {
                          void fetchSkill(s.id).then((full) => setEditing(full));
                        }}
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
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <SkillModal
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

/** 新建/编辑技能弹窗。`target="new"` 新建；传 Skill 则编辑。 */
function SkillModal({
  target,
  onClose,
  onSaved,
}: {
  target: Skill | "new" | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const isNew = target === "new";
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [body, setBody] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!target) return;
    if (target === "new") {
      setName("");
      setDescription("");
      setBody("");
    } else {
      setName(target.name);
      setDescription(target.description ?? "");
      setBody(target.body ?? "");
    }
    setErr(null);
  }, [target]);

  if (!target) return null;

  const submit = async () => {
    const n = name.trim();
    if (!n) {
      setErr("请填写技能名称");
      return;
    }
    setSaving(true);
    setErr(null);
    try {
      if (isNew) {
        await createSkill({ name: n, description: description.trim(), body });
      } else {
        await updateSkill((target as Skill).id, {
          name: n,
          description: description.trim(),
          body,
        });
      }
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
        aria-label={isNew ? "新建技能" : "编辑技能"}
        className="flex max-h-[88vh] w-full max-w-2xl flex-col rounded-panel border border-rule bg-white shadow-2xl"
      >
        <div className="flex items-center gap-2.5 border-b border-rule px-6 py-4">
          <Sparkles className="h-5 w-5 text-brand" />
          <h2 className="text-heading font-semibold text-ink">{isNew ? "新建技能" : "编辑技能"}</h2>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          <div>
            <label htmlFor="sk-name" className={labelCls}>
              技能名称 <span className="text-danger">*</span>
            </label>
            <input
              id="sk-name"
              value={name}
              autoFocus
              onChange={(e) => setName(e.target.value)}
              placeholder="如：营收口径规范"
              className={inputCls}
            />
          </div>
          <div className="mt-3.5">
            <label htmlFor="sk-desc" className={labelCls}>
              一句话描述
            </label>
            <input
              id="sk-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="如：统一营收的计算口径，避免各口径混用"
              className={inputCls}
            />
          </div>
          <div className="mt-3.5">
            <label htmlFor="sk-body" className={labelCls}>
              技能正文（Markdown，将作为指令注入大模型）
            </label>
            <textarea
              id="sk-body"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              rows={12}
              placeholder={"# 口径说明\n- 营收 = SUM(paid_amount)，不含退款\n- 时间维度统一按订单支付日\n- 输出必须标注口径来源"}
              className={`${inputCls} font-mono leading-relaxed`}
            />
          </div>

          {err && (
            <p className="mt-3.5 flex items-start gap-1.5 break-all text-small text-danger">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" /> {err}
            </p>
          )}
          <p className="mt-3.5 text-small leading-relaxed text-ink-3">
            技能只在对话中被勾选时生效，不会自动影响所有提问。
          </p>
        </div>

        <div className="flex items-center gap-2 border-t border-rule bg-canvas px-6 py-3.5">
          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={onClose}
              className="rounded-control px-3.5 py-2 text-small text-ink-3 transition hover:text-ink-2"
            >
              取消
            </button>
            <button
              onClick={() => void submit()}
              disabled={saving}
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
