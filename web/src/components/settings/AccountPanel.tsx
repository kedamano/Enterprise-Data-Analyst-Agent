import { useEffect, useRef, useState } from "react";
import {
  Camera,
  Check,
  LogIn,
  LogOut,
  Trash2,
  UserPlus,
} from "@/components/icons";
import { Avatar } from "@/components/Avatar";
import {
  Badge,
  Button,
  Field,
  Notice,
  Row,
  Section,
  SectionFooter,
  Value,
  fmtTime,
  inputCls,
  rowInputCls,
} from "./common";
import {
  login as apiLogin,
  logout as apiLogout,
  register as apiRegister,
  removeAvatar,
  roleLabel,
  updateProfile,
  uploadAvatar,
  useAuth,
} from "@/lib/user";
import type { AuthUser } from "@/lib/user";

// --------------------------------------------------------------------------- 未登录

function LoginForm({ onSwitch }: { onSwitch: () => void }) {
  const { busy } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  const submit = async () => {
    setError("");
    try {
      await apiLogin(username.trim(), password);
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败");
    }
  };

  return (
    <form
      className="space-y-3.5"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <Field label="用户名">
        <input
          className={inputCls}
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="username"
          placeholder="请输入用户名"
        />
      </Field>
      <Field label="密码">
        <input
          className={inputCls}
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          placeholder="请输入密码"
        />
      </Field>
      {error && <Notice kind="error">{error}</Notice>}
      <Button
        type="submit"
        loading={busy}
        disabled={!username.trim() || !password}
        className="w-full"
      >
        <LogIn className="h-4 w-4" /> 登录
      </Button>
      <p className="text-center text-small text-ink-3">
        还没有账号？
        <button
          type="button"
          onClick={onSwitch}
          className="ml-1 font-medium text-brand hover:text-brand"
        >
          立即注册
        </button>
      </p>
    </form>
  );
}

function RegisterForm({ onSwitch }: { onSwitch: () => void }) {
  const { busy, config } = useAuth();
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const minLen = config?.password_min_length ?? 8;
  const firstUser = config ? !config.has_users : false;

  const mismatch = confirm.length > 0 && password !== confirm;

  const submit = async () => {
    setError("");
    if (mismatch) {
      setError("两次输入的密码不一致");
      return;
    }
    try {
      await apiRegister(username.trim(), password, {
        email: email.trim(),
        display_name: displayName.trim(),
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "注册失败");
    }
  };

  return (
    <form
      className="space-y-3.5"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      {firstUser && (
        <Notice kind="info">
          数据库中还没有任何用户，<strong>第一个注册的账号将成为管理员</strong>。
          生产部署请改用 <code className="rounded bg-white/70 px-1">USER_BOOTSTRAP_ADMIN_*</code>
          配置初始管理员。
        </Notice>
      )}
      <div className="grid grid-cols-1 gap-3.5 sm:grid-cols-2">
        <Field label="用户名" hint="3–32 位字母数字">
          <input
            className={inputCls}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            placeholder="如 zhangsan"
          />
        </Field>
        <Field label="显示名称" hint="选填">
          <input
            className={inputCls}
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="如 张三"
          />
        </Field>
      </div>
      <Field label="邮箱" hint="选填">
        <input
          className={inputCls}
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="email"
          placeholder="you@company.com"
        />
      </Field>
      <div className="grid grid-cols-1 gap-3.5 sm:grid-cols-2">
        <Field label="密码" hint={`≥${minLen} 位，含字母与数字`}>
          <input
            className={inputCls}
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
          />
        </Field>
        <Field label="确认密码">
          <input
            className={inputCls}
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
          />
        </Field>
      </div>
      {mismatch && <Notice kind="error">两次输入的密码不一致</Notice>}
      {error && <Notice kind="error">{error}</Notice>}
      <Button
        type="submit"
        loading={busy}
        disabled={!username.trim() || !password || mismatch}
        className="w-full"
      >
        <UserPlus className="h-4 w-4" /> 创建账号
      </Button>
      <p className="text-center text-small text-ink-3">
        已有账号？
        <button
          type="button"
          onClick={onSwitch}
          className="ml-1 font-medium text-brand hover:text-brand"
        >
          去登录
        </button>
      </p>
    </form>
  );
}

/**
 * 未登录的登录/注册入口。
 *
 * 单列居中铺开（不套卡片）：登录表单用竖排标签读得顺，硬套行式反而别扭。
 * SettingsView 在「需要登录但没登录」时整页只渲染这一块，不显示左侧导航。
 */
export function AuthEntry() {
  const [mode, setMode] = useState<"login" | "register">("login");
  const { config } = useAuth();

  return (
    <div>
      <div>
        <h2 className="text-title font-semibold text-ink">
          {mode === "login" ? "登录" : "注册新账号"}
        </h2>
        <p className="mt-1 text-small leading-relaxed text-ink-3">
          {mode === "login"
            ? "使用用户名与密码登录。"
            : config && !config.registration_open && config.has_users
              ? "本服务已关闭公开注册，请联系管理员开通。"
              : "创建后即可使用。"}
        </p>
        <div className="mt-5">
          {mode === "login" ? (
            <LoginForm onSwitch={() => setMode("register")} />
          ) : (
            <RegisterForm onSwitch={() => setMode("login")} />
          )}
        </div>
      </div>

    </div>
  );
}

// --------------------------------------------------------------------------- 已登录：资料

/** 头像行：自带上传/移除的状态，避免把文件选择塞进资料表单里。 */
function AvatarRow({ user }: { user: AuthUser }) {
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const canRemove = Boolean(user.avatar);

  const pick = async (file: File | undefined) => {
    if (!file) return;
    setError("");
    setUploading(true);
    try {
      await uploadAvatar(file);
    } catch (err) {
      setError(err instanceof Error ? err.message : "头像上传失败");
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  return (
    <>
      <Row
        label="头像"
        action={
          <div className="flex items-center gap-1.5">
            <Button
              variant="secondary"
              loading={uploading}
              onClick={() => fileRef.current?.click()}
            >
              <Camera className="h-4 w-4" /> 更换
            </Button>
            {canRemove && (
              <Button
                variant="ghost"
                onClick={async () => {
                  setError("");
                  try {
                    await removeAvatar();
                  } catch (err) {
                    setError(err instanceof Error ? err.message : "移除失败");
                  }
                }}
              >
                <Trash2 className="h-4 w-4" /> 移除
              </Button>
            )}
          </div>
        }
      >
        <div className="flex items-center gap-3">
          <Avatar user={user} size="lg" />
          <span className="text-micro leading-relaxed text-ink-3">
            PNG / JPG / WEBP
            <br />
            ≤4 MB
          </span>
        </div>
        <input
          ref={fileRef}
          type="file"
          accept="image/png,image/jpeg,image/webp,image/gif"
          className="hidden"
          onChange={(e) => void pick(e.target.files?.[0])}
        />
      </Row>
      {error && (
        <div className="pb-3">
          <Notice kind="error">{error}</Notice>
        </div>
      )}
    </>
  );
}

/** 账号信息分区：只读事实 + 头像维护。 */
function AccountInfo({ user }: { user: AuthUser }) {
  return (
    <Section
      title="账号信息"
      desc="用于登录与团队识别的身份信息。用户名与账号来源由系统记录，不可修改。"
    >
      <AvatarRow user={user} />
      <Row
        label="用户名"
        value={
          <span className="inline-flex items-baseline gap-2">
            {user.username}
            <span className="text-micro text-ink-3">不可修改</span>
          </span>
        }
      />
      <Row
        label="角色"
        value={<Badge tone="brand">{roleLabel(user.role)}</Badge>}
      />
      {user.status !== "active" && (
        <Row label="状态" value={<Badge tone="danger">已停用</Badge>} />
      )}
      <Row label="租户" value={<Value empty="未分配">{user.tenant}</Value>} />
      <Row label="注册时间" value={fmtTime(user.created_at)} />
      <Row
        label="最近登录"
        value={user.last_login_at ? fmtTime(user.last_login_at) : "从未登录"}
      />
    </Section>
  );
}

function ProfileEditor({ user }: { user: AuthUser }) {
  const [displayName, setDisplayName] = useState(user.display_name);
  const [email, setEmail] = useState(user.email);
  const [phone, setPhone] = useState(user.phone);
  const [bio, setBio] = useState(user.bio);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  // 切换账号/资料被外部刷新时同步表单，避免停留在旧值
  useEffect(() => {
    setDisplayName(user.display_name);
    setEmail(user.email);
    setPhone(user.phone);
    setBio(user.bio);
  }, [user.id, user.display_name, user.email, user.phone, user.bio]);

  const dirty =
    displayName !== user.display_name ||
    email !== user.email ||
    phone !== user.phone ||
    bio !== user.bio;

  const save = async () => {
    setError("");
    setSaving(true);
    try {
      await updateProfile({ display_name: displayName, email, phone, bio });
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const reset = () => {
    setDisplayName(user.display_name);
    setEmail(user.email);
    setPhone(user.phone);
    setBio(user.bio);
  };

  return (
    <Section
      title="个人资料"
      desc="昵称会显示在团队列表与登录记录里。修改后需要保存才生效。"
    >
      <Row label="显示名称">
        <input
          className={rowInputCls}
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          placeholder="你的名字"
        />
      </Row>
      <Row label="邮箱">
        <input
          className={rowInputCls}
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="you@company.com"
        />
      </Row>
      <Row label="手机号">
        <input
          className={rowInputCls}
          value={phone}
          onChange={(e) => setPhone(e.target.value)}
          placeholder="选填"
        />
      </Row>
      <Row label="简介" align="start">
        <textarea
          className={`${rowInputCls} min-h-[72px] resize-y`}
          value={bio}
          maxLength={200}
          onChange={(e) => setBio(e.target.value)}
          placeholder="一句话描述你的职责，如「华东区数据分析负责人」"
        />
      </Row>
      {error && (
        <div className="pt-3">
          <Notice kind="error">{error}</Notice>
        </div>
      )}
      {/* 无改动时不占位——行式布局里常驻一排灰按钮很吵 */}
      {dirty && (
        <SectionFooter>
          <Button onClick={() => void save()} loading={saving}>
            保存修改
          </Button>
          <Button variant="ghost" onClick={reset}>
            撤销
          </Button>
        </SectionFooter>
      )}
      {saved && (
        <SectionFooter>
          <span className="inline-flex items-center gap-1 text-small font-medium text-verified">
            <Check className="h-4 w-4" /> 已保存
          </span>
        </SectionFooter>
      )}
    </Section>
  );
}

/** 会话分区：退出登录。 */
function SessionSection() {
  const [busy, setBusy] = useState(false);
  return (
    <Section title="会话" desc="退出后本机不再保留登录令牌，需要重新登录才能继续使用。">
      <Row
        label="退出登录"
        value="结束当前设备的登录状态"
        action={
          <Button
            variant="secondary"
            loading={busy}
            onClick={async () => {
              setBusy(true);
              await apiLogout();
              setBusy(false);
            }}
          >
            <LogOut className="h-4 w-4" /> 退出登录
          </Button>
        }
      />
    </Section>
  );
}

// --------------------------------------------------------------------------- 入口

export function AccountPanel() {
  const { user, ready } = useAuth();

  if (!ready) {
    return <p className="py-8 text-center text-small text-ink-3">正在读取登录状态…</p>;
  }

  // 认证关闭的部署走这里（user 恒为 null）：导航照常显示，账号页给登录入口，
  // 因为那个部署下虽然登录不了，但别的分区（体验/权限）仍然要能进。
  if (!user) return <AuthEntry />;

  return (
    <div className="space-y-4">
      <AccountInfo user={user} />
      <ProfileEditor user={user} />
      <SessionSection />
    </div>
  );
}
