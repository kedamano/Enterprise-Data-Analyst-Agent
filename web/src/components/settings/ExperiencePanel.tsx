import { useEffect, useState } from "react";
import { Cpu, Palette, ShieldCheck, Trash2, Zap } from "lucide-react";
import {
  Badge,
  Button,
  Loading,
  Notice,
  Row,
  Section,
  Value,
} from "./common";
import { fetchHealth } from "@/lib/api";
import type { HealthInfo } from "@/lib/api";
import { useLocalStorage } from "@/lib/storage";
import { useAuth } from "@/lib/user";

/** 行尾开关：行式布局里只用开关本体，不再套一层带边框的盒子。 */
function Switch({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
}) {
  return (
    <label className="relative inline-flex cursor-pointer items-center">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="peer sr-only"
        aria-label={label}
      />
      <span className="block h-5 w-9 rounded-full bg-rule transition peer-checked:bg-brand" />
      <span className="pointer-events-none absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white shadow transition peer-checked:translate-x-4" />
    </label>
  );
}

/**
 * 主数据源的「人话」标签。
 *
 * 直接把原始 DSN（`sqlite:///./data/sample_enterprise.db`）摊在界面上是部署者的语言，
 * 业务用户读不出任何信息。这里按方言给出可读名称，原始连接串收敛进 hover 提示——
 * 需要排查问题时依然拿得到，但日常阅读路径上不出现。
 */
function SourceLabel({ dsn }: { dsn: string }) {
  const raw = (dsn || "").trim();
  if (!raw) return <span className="text-ink-3">未配置</span>;

  const scheme = (/^([a-z0-9+]+):\/\//i.exec(raw)?.[1] ?? "").toLowerCase();
  const rest = raw.replace(/^[a-z0-9+]+:\/\//i, "");

  const DIALECT: Record<string, string> = {
    sqlite: "本地 SQLite 文件",
    sqlite3: "本地 SQLite 文件",
    postgres: "PostgreSQL",
    postgresql: "PostgreSQL",
    mysql: "MySQL",
    mariadb: "MariaDB",
    mssql: "SQL Server",
    oracle: "Oracle",
  };
  const label = DIALECT[scheme] ?? (scheme ? scheme.toUpperCase() : "数据库");

  // 副标识取库名/文件名：sqlite 是路径末段，其余取最后一个 path 段
  const detail =
    (/[/\\?#]/.exec(rest)
      ? rest.split(/[/\\?#]/).filter(Boolean).pop()
      : rest.split("@").pop()) ?? "";

  return (
    <span className="inline-flex min-w-0 flex-col">
      <span className="truncate">{label}</span>
      {detail && (
        <span className="truncate font-mono text-micro text-ink-3" title={raw}>
          {detail}
        </span>
      )}
    </span>
  );
}

export function ExperiencePanel({ onClearAll }: { onClearAll: () => void }) {
  const { config } = useAuth();
  const [info, setInfo] = useState<HealthInfo | null>(null);
  const [failed, setFailed] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [reduceMotion, setReduceMotion] = useLocalStorage<boolean>(
    "da_reduce_motion",
    false,
  );

  useEffect(() => {
    const ctrl = new AbortController();
    fetchHealth(ctrl.signal)
      .then(setInfo)
      .catch(() => {
        // StrictMode 下 effect 会跑两遍，第一遍的请求被 cleanup abort 掉。
        // 那不是「连不上」——不过滤掉，这条假报错会一直挂在界面上
        // （成功分支只 setInfo，不会把它清回 false）。
        if (!ctrl.signal.aborted) setFailed(true);
      });
    return () => ctrl.abort();
  }, []);

  // 与全局 CSS 约定：`[data-reduce-motion="1"]` 关掉过渡/动画
  useEffect(() => {
    document.documentElement.setAttribute(
      "data-reduce-motion",
      reduceMotion ? "1" : "0",
    );
  }, [reduceMotion]);

  return (
    <div className="space-y-2">
      <Section title="界面体验" desc="仅影响本机浏览器。">
        <Row
          label="减少界面动效"
          action={
            <Switch
              checked={reduceMotion}
              onChange={setReduceMotion}
              label="减少界面动效"
            />
          }
        >
          <span className="text-ink-3">
            关闭入场动画与过渡效果，适合对动效敏感或追求极致响应速度的场景。
          </span>
        </Row>
      </Section>

      <Section title="服务运行信息" desc="来自后端 /health，用于排查环境问题。">
        {failed ? (
          <div className="pt-3.5">
            <Notice kind="error">无法连接后端服务，请确认后端已启动。</Notice>
          </div>
        ) : !info ? (
          <Loading what="读取服务状态" />
        ) : (
          <>
            <Row
              label="服务状态"
              value={
                <span className="inline-flex items-center gap-1.5">
                  <span className="h-1.5 w-1.5 rounded-full bg-verified" />
                  {info.status}
                </span>
              }
            />
            <Row label="主数据源" value={<SourceLabel dsn={info.data_source} />} />
            <Row
              label="模型状态"
              value={
                info.llm_degraded ? (
                  <span className="inline-flex items-center gap-1.5">
                    降级中 <Badge tone="attention">结论可信度下降</Badge>
                  </span>
                ) : info.mock_llm ? (
                  <span className="inline-flex items-center gap-1.5">
                    模拟输出 <Badge tone="attention">仅供演示</Badge>
                  </span>
                ) : (
                  "正常"
                )
              }
            />
            <Row
              label="知识库"
              value={info.knowledge_enabled ? "已启用" : "未启用"}
            />
            <Row
              label="命名数据源"
              value={`${info.data_sources?.length ?? 0} 个`}
            />
          </>
        )}
      </Section>

      <Section title="账号与鉴权" desc="决定本服务是否需要登录才能访问。">
        <Row
          label="用户账号体系"
          value={
            <Value empty="未启用">
              {config?.user_auth_enabled ? "已启用" : ""}
            </Value>
          }
        />
        <Row
          label="接口鉴权强制"
          value={
            config?.enforcement ? (
              <span className="text-verified">已开启</span>
            ) : (
              <span className="text-attention">未开启（接口对外开放）</span>
            )
          }
        />
        <Row
          label="开放注册"
          value={config?.registration_open ? "允许" : "已关闭"}
        />
        {config && !config.enforcement && (
          <div className="pt-3">
            {/* 原先这里直接展示环境变量名 AUTH_ENABLED=false —— 那是部署者的语言。
                使用者只需要知道：现在没开、会怎样、找谁开。 */}
            <Notice kind="info">
              当前未开启登录校验，未登录也能调用全部接口。对外提供服务前请联系管理员开启，
              否则账号与角色只是界面上的设置，拦不住任何人。
            </Notice>
          </div>
        )}
      </Section>

      <Section title="本地数据">
        <Row
          label="对话历史"
          action={
            <Button variant="danger" onClick={() => setConfirmClear(true)}>
              <Trash2 className="h-4 w-4" /> 清空对话历史
            </Button>
          }
        >
          <span className="text-ink-3">
            会话保存在浏览器 localStorage 中（不上传服务端）。
            清空后无法恢复，服务端的分析产物与知识库不受影响。
          </span>
        </Row>
        {confirmClear && (
          <div className="mt-3 flex items-center gap-2 rounded-control border border-danger bg-danger-soft px-3 py-2">
            <span className="text-small text-danger">
              确认删除全部本地会话？此操作不可撤销。
            </span>
            <Button
              variant="danger"
              className="ml-auto"
              onClick={() => {
                onClearAll();
                setConfirmClear(false);
              }}
            >
              确认删除
            </Button>
            <Button variant="secondary" onClick={() => setConfirmClear(false)}>
              取消
            </Button>
          </div>
        )}
      </Section>

      <Section title="关于">
        {[
          {
            icon: Cpu,
            title: "六阶段编排",
            desc: "意图理解 → 计划 → 取数 → 分析 → 质检 → 报告",
          },
          {
            icon: Zap,
            title: "真实工具调用",
            desc: "SQL 查询、图表生成、混合检索，全部可溯源",
          },
          {
            icon: ShieldCheck,
            title: "权限内建",
            desc: "角色、表/列级访问与行级过滤在服务端强制",
          },
        ].map(({ icon: Icon, title, desc }) => (
          <Row
            key={title}
            label={title}
            value={
              <span className="inline-flex items-center gap-2">
                <Icon className="h-4 w-4 shrink-0 text-brand" />
                {desc}
              </span>
            }
          />
        ))}
        <p className="flex items-center gap-1.5 pt-3.5 text-micro text-ink-3">
          <Palette className="h-4 w-4" /> Enterprise Data Analyst Agent · 本地开发版本
        </p>
      </Section>
    </div>
  );
}
