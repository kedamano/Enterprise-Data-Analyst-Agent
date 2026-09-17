"""AUTH/02 用户账号体系：注册、登录、资料、角色。

与 AUTH/01 的关系
-----------------
AUTH/01 解决的是「**谁能调这个 API**」——身份来自配置里的静态 `X-API-Key`
（`AUTH_KEYS` JSON）。那套东西适合机器对机器（CI、外部 Agent），但**没有用户概念**：
没有注册、没有密码、没有头像、改不了角色、登不出去。

AUTH/02 补的就是这一层：**人**。每个用户有账号密码、资料、头像、角色；
登录后签发**可吊销的会话令牌**；令牌 ⇄ `Principal`（复用 AUTH/01 的数据权限模型，
所以表/列/行三层权限、工具级 RBAC、审计**一行都不用改**）。

设计取舍（都是刻意的）
--------------------
- **零新依赖**：密码用标准库 ``hashlib.scrypt``（n=2^14, r=8, p=1，16 MiB）。
  不引 passlib/bcrypt/argon2——本机实测这些包都不在 venv 里，而 scrypt 本身是
  内存硬的 KBKDF，比 PBKDF2 抗 GPU，比 bcrypt 抗 ASIC，够用且不用装东西。
- **不用 JWT 做会话**：JWT 无法吊销（改密码/踢设备要等过期）。改用
  ``secrets.token_urlsafe(32)`` 存库 + 过期时间，**登录态可立即撤销**——
  这是"登出"和"改密后踢掉其他设备"能真做到的前提。
  哈希后才落库（库里泄露 token 明文等于泄露登录态）。
- **密码策略可配，但有下限**：默认 ≥8 位且必须同时含字母和数字。
  下限不可配到 0（配置写错不该把口令体系废掉，同 AUTH/01 的"配置错了要吵"）。
- **登录失败计数与锁定**：按 `username` 累计失败，超阈值锁一段时间。
  只记失败不记成功——成功不需要节流（且成功路径已在 AUTH/01 有配额）。
- **首个注册者成为管理员**（可关）：自托管控制台若没有初始管理员，
  就永远没人能改角色。这个行为**明确记日志**，不静默。生产请配
  `USER_BOOTSTRAP_ADMIN_USERNAME/PASSWORD` 并关掉本开关。
- **管理员不能自降级/自停用**：否则最后一个管理员一失手就锁死系统。
  同理禁止停用/降级"库里最后一个 active 的管理员"。
- **物理删除一律走 `safe_fs.purge_*`**：本机沙箱的 safe-delete 守卫会抛
  `SystemExit`（`BaseException`，穿透 uvicorn 直接杀死进程）。头像替换/用户删除
  都是物理删除点，必须用那个"绝不外泄异常"的封装。见 `app/core/safe_fs.py`。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ...config import get_settings
from ..safe_fs import purge_file

logger = logging.getLogger("da.auth.users")

# --------------------------------------------------------------------------- #
# 角色 —— 与 AUTH/01 的 `ROLE_PERMISSIONS`（tools/specs.py）**同一套**。
# 这里只加人类可读的展示信息，权限集合的唯一事实来源仍在 specs.py，
# 避免"两处各写一份、改了边漏改"。
# --------------------------------------------------------------------------- #
ROLES: tuple[str, ...] = ("viewer", "analyst", "analyst_lead", "admin")

ROLE_META: dict[str, dict[str, str]] = {
    "viewer": {
        "label": "查看者",
        "summary": "可查元数据与业务知识，不能读取业务数据。",
    },
    "analyst": {
        "label": "分析师",
        "summary": "可读取业务数据并执行计算（Python / 可视化）。",
    },
    "analyst_lead": {
        "label": "分析主管",
        "summary": "在分析师基础上可生成并交付报告。",
    },
    "admin": {
        "label": "管理员",
        "summary": "全部工具权限，并可管理用户与角色。",
    },
}

# 角色强弱排序（越大越强）。用于"不能把用户改成比自己更高的角色"这类约束。
ROLE_RANK: dict[str, int] = {r: i for i, r in enumerate(ROLES)}

DEFAULT_ROLE = "analyst"

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
_VALID_STATUS = (STATUS_ACTIVE, STATUS_DISABLED)

# --------------------------------------------------------------------------- #
# 密码哈希
# --------------------------------------------------------------------------- #
_SCRYPT_N = 2 ** 14      # 16384 → 128*N*r = 16 MiB
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32
_SALT_LEN = 16
_ALGO = "scrypt"

# 密码下限：不可配置到低于这个值
PASSWORD_MIN_HARD_FLOOR = 8


def hash_password(password: str) -> str:
    """``scrypt$n$r$p$<salt_b64>$<hash_b64>`` —— 参数随哈希一起存。

    把参数写进字符串而不是硬编码在校验里，是为了**以后能调高成本而不失效旧密码**：
    校验时用行内参数，验证通过后可按需重算升级。
    """
    salt = secrets.token_bytes(_SALT_LEN)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_LEN,
    )
    return "{}${}${}${}${}${}".format(
        _ALGO, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    """常数时间校验。任何解析失败都返回 False（绝不抛，也不回显差异）。"""
    try:
        algo, n, r, p, salt_b64, hash_b64 = (stored or "").split("$")
        if algo != _ALGO:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=salt,
            n=int(n), r=int(r), p=int(p), dklen=len(expected),
        )
        return hmac.compare_digest(dk, expected)
    except BaseException:  # noqa: BLE001 —— 校验路径不许因脏数据抛
        return False


def password_policy_error(password: str, *, min_length: int | None = None) -> Optional[str]:
    """返回不合规原因，合规返回 None。"""
    pwd = password or ""
    floor = max(PASSWORD_MIN_HARD_FLOOR, int(min_length or PASSWORD_MIN_HARD_FLOOR))
    if len(pwd) < floor:
        return f"密码至少 {floor} 位"
    if len(pwd) > 128:
        return "密码最长 128 位"
    if not re.search(r"[A-Za-z]", pwd):
        return "密码需包含字母"
    if not re.search(r"\d", pwd):
        return "密码需包含数字"
    if pwd.strip() != pwd:
        return "密码首尾不能有空白字符"
    return None


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _token_fingerprint(token: str) -> str:
    """库里只存 token 的 SHA-256 —— 库泄露不等于登录态泄露。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 用户名 / 邮箱
# --------------------------------------------------------------------------- #
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_PHONE_RE = re.compile(r"^[0-9+\-() ]{5,20}$")


def username_error(username: str) -> Optional[str]:
    u = (username or "").strip()
    if not u:
        return "用户名不能为空"
    if not _USERNAME_RE.match(u):
        return "用户名需为 3–32 位的字母、数字、下划线、点或连字符"
    return None


def email_error(email: str) -> Optional[str]:
    e = (email or "").strip()
    if not e:
        return None  # 邮箱选填
    return None if _EMAIL_RE.match(e) else "邮箱格式不正确"


def phone_error(phone: str) -> Optional[str]:
    p = (phone or "").strip()
    if not p:
        return None
    return None if _PHONE_RE.match(p) else "手机号格式不正确"


class UserError(Exception):
    """带 HTTP 状态码的业务错误（路由层直接转成响应）。"""

    def __init__(self, message: str, status_code: int = 400, code: str = "invalid"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #
_lock = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email           TEXT NOT NULL DEFAULT '',
    phone           TEXT NOT NULL DEFAULT '',
    display_name    TEXT NOT NULL DEFAULT '',
    bio             TEXT NOT NULL DEFAULT '',
    password_hash   TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL DEFAULT 'analyst',
    status          TEXT NOT NULL DEFAULT 'active',
    tenant          TEXT NOT NULL DEFAULT '',
    avatar          TEXT NOT NULL DEFAULT '',
    avatar_version  INTEGER NOT NULL DEFAULT 0,
    source          TEXT NOT NULL DEFAULT 'password',
    wechat_openid   TEXT NOT NULL DEFAULT '',
    wechat_unionid  TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT '',
    last_login_at   TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_wechat_openid
    ON users(wechat_openid) WHERE wechat_openid <> '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_wechat_unionid
    ON users(wechat_unionid) WHERE wechat_unionid <> '';

CREATE TABLE IF NOT EXISTS sessions (
    token_fp    TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0,
    user_agent  TEXT NOT NULL DEFAULT '',
    ip          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS login_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL DEFAULT '',
    user_id  TEXT NOT NULL DEFAULT '',
    ip       TEXT NOT NULL DEFAULT '',
    ts       REAL NOT NULL,
    ok       INTEGER NOT NULL DEFAULT 0,
    reason   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_login_events ON login_events(username, ts);
"""

_PUBLIC_FIELDS = (
    "id", "username", "email", "phone", "display_name", "bio", "role",
    "status", "tenant", "avatar", "avatar_version", "source",
    "created_at", "updated_at", "last_login_at",
)


class UserStore:
    """SQLite 用户库。单文件、线程安全（每次操作开连接 + 模块级锁）。"""

    def __init__(self, db_path: str | Path | None = None,
                 avatar_dir: str | Path | None = None) -> None:
        settings = get_settings()
        self.db = Path(db_path or getattr(settings, "user_db_path", "") or "data/users.db")
        self.avatar_dir = Path(
            avatar_dir or getattr(settings, "user_avatar_dir", "") or "data/avatars"
        )
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.avatar_dir.mkdir(parents=True, exist_ok=True)
        with _lock, sqlite3.connect(self.db) as c:
            c.executescript(_SCHEMA)

    # ---------------------------------------------------------------- 内部
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_public(row: sqlite3.Row | dict) -> dict[str, Any]:
        d = dict(row)
        return {k: d.get(k) for k in _PUBLIC_FIELDS}

    def _log_event(self, c: sqlite3.Connection, username: str, user_id: str,
                   ok: bool, reason: str = "", ip: str = "") -> None:
        try:
            c.execute(
                "INSERT INTO login_events(username, user_id, ip, ts, ok, reason) "
                "VALUES(?,?,?,?,?,?)",
                (username or "", user_id or "", ip or "", _now().timestamp(),
                 1 if ok else 0, reason or ""),
            )
        except Exception:
            pass  # 记流水失败不打断登录

    # ---------------------------------------------------------------- 查询
    def get(self, user_id: str) -> Optional[dict[str, Any]]:
        with _lock, self._connect() as c:
            row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return self._row_to_public(row) if row else None

    def get_by_username(self, username: str) -> Optional[dict[str, Any]]:
        with _lock, self._connect() as c:
            row = c.execute(
                "SELECT * FROM users WHERE username=? COLLATE NOCASE",
                ((username or "").strip(),),
            ).fetchone()
        return dict(row) if row else None

    def get_by_openid(self, openid: str, unionid: str = "") -> Optional[dict[str, Any]]:
        """优先按 unionid 命中（同主体下多应用同一人），退回 openid。"""
        if not openid and not unionid:
            return None
        with _lock, self._connect() as c:
            row = None
            if unionid:
                row = c.execute(
                    "SELECT * FROM users WHERE wechat_unionid=?", (unionid,)
                ).fetchone()
            if row is None and openid:
                row = c.execute(
                    "SELECT * FROM users WHERE wechat_openid=?", (openid,)
                ).fetchone()
        return dict(row) if row else None

    def count(self) -> int:
        with _lock, self._connect() as c:
            return int(c.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def count_admins(self, *, only_active: bool = True) -> int:
        sql = "SELECT COUNT(*) FROM users WHERE role='admin'"
        if only_active:
            sql += " AND status='active'"
        with _lock, self._connect() as c:
            return int(c.execute(sql).fetchone()[0])

    def list_users(self, *, q: str = "", role: str = "", status: str = "",
                   limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        where, params = [], []
        if q.strip():
            where.append("(username LIKE ? OR display_name LIKE ? OR email LIKE ?)")
            like = f"%{q.strip()}%"
            params += [like, like, like]
        if role:
            where.append("role=?")
            params.append(role)
        if status:
            where.append("status=?")
            params.append(status)
        sql = "SELECT * FROM users"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
        with _lock, self._connect() as c:
            rows = c.execute(sql, params).fetchall()
        return [self._row_to_public(r) for r in rows]

    # ---------------------------------------------------------------- 注册
    def create_user(
        self,
        username: str,
        password: str,
        *,
        email: str = "",
        phone: str = "",
        display_name: str = "",
        role: str | None = None,
        tenant: str = "",
        source: str = "password",
        wechat_openid: str = "",
        wechat_unionid: str = "",
    ) -> dict[str, Any]:
        """创建用户。校验不通过 / 重名 → UserError。"""
        settings = get_settings()
        err = username_error(username)
        if err:
            raise UserError(err, 400, "invalid_username")
        err = email_error(email) or phone_error(phone)
        if err:
            raise UserError(err, 400, "invalid_profile")

        username = username.strip()
        # 微信登录建立的新账号没有密码（只能扫码登录），此时跳过策略校验
        if source == "password":
            perr = password_policy_error(
                password, min_length=getattr(settings, "password_min_length", 8)
            )
            if perr:
                raise UserError(perr, 400, "weak_password")

        if role is None:
            # 库里一个用户都没有 → 首个注册者接管（自托管场景的唯一出路）
            first = self.count() == 0 and bool(
                getattr(settings, "user_first_registrant_is_admin", True)
            )
            role = "admin" if first else DEFAULT_ROLE
        if role not in ROLES:
            raise UserError(f"未知角色 {role!r}", 400, "invalid_role")

        now = _iso(_now())
        uid = "u_" + secrets.token_hex(8)
        pw_hash = hash_password(password) if password else ""
        try:
            with _lock, self._connect() as c:
                c.execute(
                    "INSERT INTO users(id, username, email, phone, display_name, bio,"
                    " password_hash, role, status, tenant, avatar, avatar_version,"
                    " source, wechat_openid, wechat_unionid, created_at, updated_at,"
                    " last_login_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uid, username, email.strip(), phone.strip(),
                     display_name.strip() or username, "", pw_hash, role,
                     STATUS_ACTIVE, tenant, "", 0, source,
                     wechat_openid, wechat_unionid, now, now, ""),
                )
        except sqlite3.IntegrityError as exc:
            low = str(exc).lower()
            if "username" in low:
                raise UserError("用户名已被占用", 409, "username_taken") from exc
            if "wechat" in low:
                raise UserError("该微信已绑定其他账号", 409, "wechat_taken") from exc
            raise UserError("账号创建失败（唯一性冲突）", 409, "conflict") from exc

        if role == "admin" and self.count() == 1:
            logger.warning(
                "已创建初始管理员 %r —— 请立即登录并修改密码；"
                "生产环境建议改用 USER_BOOTSTRAP_ADMIN_* 并关闭 "
                "USER_FIRST_REGISTRANT_IS_ADMIN", username,
            )
        return self.get(uid) or {}

    # ---------------------------------------------------------------- 登录
    def _recent_failures(self, c: sqlite3.Connection, username: str,
                         window_s: int) -> int:
        """窗口内的**凭据失败**次数，用于判定是否该锁定。

        ⚠️ 必须排除 ``reason='locked'``。锁定分支每次被触发都会记一条 ``locked``
        事件，如果它也算"失败"，那么攻击者只要持续尝试，每一条 ``locked`` 都会把
        窗口往后推——受害者的账号会被**永久**锁死，连正确口令都进不去（经典的
        「锁定型 DoS」）。排除之后，窗口锚定在**真实凭据失败**上：
        连续打也就能拿到 max_fail 次/窗口，锁定到点自解。
        """
        cutoff = _now().timestamp() - window_s
        return int(c.execute(
            "SELECT COUNT(*) FROM login_events WHERE username=? COLLATE NOCASE "
            "AND ok=0 AND reason<>'locked' AND ts>=?",
            (username, cutoff),
        ).fetchone()[0])

    def authenticate(self, username: str, password: str, *, ip: str = "") -> dict[str, Any]:
        """校验账号密码。失败抛 UserError（**不回显是哪一项错**，防枚举）。

        ⚠️ 两条容易写错的地方（此前就踩了，见下）：

        1. **事件写入必须与 raise 分处两个事务**。`sqlite3.Connection` 的
           ``__exit__`` 在异常传播时会 **rollback**——若把 ``_log_event`` 和
           ``raise`` 放进同一个 ``with`` 块，"锁定"事件会被静默回滚掉，
           表里永远查不到。所以下面所有分支都是「先独立事务写事件，再在块外抛」。
        2. **锁定事件要归属到账号**。``_log_event(..., user_id="")`` 会让该行
           逃过 ``WHERE user_id=?`` 的过滤，于是受害者在自己的登录流水里看不到
           "我的号正被暴力破解"——这正是该功能存在的意义。故这里先解析用户，
           拿到 id 再记。
        """
        settings = get_settings()
        max_fail = int(getattr(settings, "user_login_max_failures", 8))
        lock_s = int(getattr(settings, "user_login_lockout_s", 300))
        uname = (username or "").strip()

        # 先解析用户：锁定分支需要 user_id 才能让受害者自查到异常尝试。
        # 用户名不存在时 row=None，此时归属为空是唯一合理选择（无法归属）。
        row = self.get_by_username(uname)

        if max_fail > 0:
            with _lock, self._connect() as c:
                recent = self._recent_failures(c, uname, lock_s)
            if recent >= max_fail:
                with _lock, self._connect() as c:
                    self._log_event(c, uname, row["id"] if row else "",
                                    False, "locked", ip)
                raise UserError(
                    f"登录失败次数过多，请 {max(1, lock_s // 60)} 分钟后再试",
                    429, "locked",
                )

        bad = UserError("用户名或密码错误", 401, "bad_credentials")
        if not row:
            with _lock, self._connect() as c:
                self._log_event(c, uname, "", False, "no_user", ip)
            raise bad
        if row.get("status") != STATUS_ACTIVE:
            with _lock, self._connect() as c:
                self._log_event(c, uname, row["id"], False, "disabled", ip)
            raise UserError("账号已被停用，请联系管理员", 403, "disabled")
        if not row.get("password_hash"):
            with _lock, self._connect() as c:
                self._log_event(c, uname, row["id"], False, "no_password", ip)
            raise UserError("该账号通过微信创建，请使用微信扫码登录", 400,
                            "wechat_only")
        if not verify_password(password, row["password_hash"]):
            with _lock, self._connect() as c:
                self._log_event(c, uname, row["id"], False, "bad_password", ip)
            raise bad

        with _lock, self._connect() as c:
            self._log_event(c, uname, row["id"], True, "ok", ip)
        return self._row_to_public(row)

    def touch_login(self, user_id: str) -> None:
        now = _iso(_now())
        with _lock, self._connect() as c:
            c.execute("UPDATE users SET last_login_at=?, updated_at=? WHERE id=?",
                      (now, now, user_id))

    # ---------------------------------------------------------------- 会话
    def issue_token(self, user_id: str, *, user_agent: str = "", ip: str = "") -> dict[str, Any]:
        settings = get_settings()
        ttl_h = float(getattr(settings, "session_ttl_hours", 72) or 72)
        ttl_h = max(0.25, min(ttl_h, 24 * 365))
        tok = new_token()
        now = _now()
        exp = now + timedelta(hours=ttl_h)
        with _lock, self._connect() as c:
            c.execute(
                "INSERT INTO sessions(token_fp, user_id, created_at, expires_at,"
                " revoked, user_agent, ip) VALUES(?,?,?,?,0,?,?)",
                (_token_fingerprint(tok), user_id, _iso(now), _iso(exp),
                 (user_agent or "")[:300], (ip or "")[:64]),
            )
        return {"token": tok, "expires_at": _iso(exp)}

    def resolve_token(self, token: str) -> Optional[dict[str, Any]]:
        """token → user（过期/撤销/停用都返回 None）。"""
        if not token:
            return None
        fp = _token_fingerprint(token)
        with _lock, self._connect() as c:
            row = c.execute(
                "SELECT * FROM sessions WHERE token_fp=?", (fp,)
            ).fetchone()
        if not row or row["revoked"]:
            return None
        exp = _parse_iso(row["expires_at"])
        if exp is None or exp <= _now():
            self.revoke_token(token)
            return None
        user = self.get(row["user_id"])
        if not user or user.get("status") != STATUS_ACTIVE:
            return None
        return user

    def revoke_token(self, token: str) -> bool:
        if not token:
            return False
        with _lock, self._connect() as c:
            cur = c.execute(
                "UPDATE sessions SET revoked=1 WHERE token_fp=? AND revoked=0",
                (_token_fingerprint(token),),
            )
            return cur.rowcount > 0

    def revoke_user_sessions(self, user_id: str, *, keep_token: str = "") -> int:
        """踢掉某用户的所有会话（改密后调用）。`keep_token` 可保留当前这台设备。"""
        keep_fp = _token_fingerprint(keep_token) if keep_token else ""
        with _lock, self._connect() as c:
            cur = c.execute(
                "UPDATE sessions SET revoked=1 WHERE user_id=? AND revoked=0"
                + (" AND token_fp<>?" if keep_fp else ""),
                (user_id, keep_fp) if keep_fp else (user_id,),
            )
            return int(cur.rowcount)

    def list_sessions(self, user_id: str) -> list[dict[str, Any]]:
        with _lock, self._connect() as c:
            rows = c.execute(
                "SELECT created_at, expires_at, user_agent, ip FROM sessions "
                "WHERE user_id=? AND revoked=0 ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        out = []
        for r in rows:
            exp = _parse_iso(r["expires_at"])
            if exp and exp > _now():
                out.append(dict(r))
        return out

    def purge_expired(self) -> int:
        with _lock, self._connect() as c:
            cur = c.execute(
                "DELETE FROM sessions WHERE revoked=1 OR expires_at < ?",
                (_iso(_now()),),
            )
            return int(cur.rowcount)

    def login_event_summary(self, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        with _lock, self._connect() as c:
            rows = c.execute(
                "SELECT username, ip, ts, ok, reason FROM login_events "
                "WHERE user_id=? ORDER BY ts DESC LIMIT ?",
                (user_id, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 资料
    def update_profile(self, user_id: str, **fields: Any) -> dict[str, Any]:
        """只允许改白名单字段；未知字段忽略。"""
        allowed = {"display_name", "email", "phone", "bio", "avatar"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "email":
                err = email_error(str(v))
                if err:
                    raise UserError(err, 400, "invalid_email")
            if k == "phone":
                err = phone_error(str(v))
                if err:
                    raise UserError(err, 400, "invalid_phone")
            if k == "display_name":
                v = str(v).strip()
                if len(v) > 40:
                    raise UserError("昵称最长 40 字", 400, "invalid_display_name")
            if k == "bio":
                v = str(v).strip()
                if len(v) > 200:
                    raise UserError("简介最长 200 字", 400, "invalid_bio")
            sets.append(f"{k}=?")
            params.append(str(v).strip() if k != "avatar" else str(v))
        if not sets:
            return self.get(user_id) or {}
        sets.append("updated_at=?")
        params.append(_iso(_now()))
        params.append(user_id)
        with _lock, self._connect() as c:
            c.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", params)
        return self.get(user_id) or {}

    def change_password(self, user_id: str, old_password: str, new_password: str,
                        *, keep_token: str = "") -> int:
        """改密并踢掉其他会话（返回被撤销的会话数）。

        要求提供旧密码：本机是同一个浏览器在操作，但"已登录即免旧密码"会让
        一次会话劫持升级成永久账号劫持。
        """
        settings = get_settings()
        row = self.get_by_username((self.get(user_id) or {}).get("username", ""))
        if not row:
            raise UserError("用户不存在", 404, "not_found")
        if row.get("password_hash") and not verify_password(old_password, row["password_hash"]):
            raise UserError("原密码不正确", 400, "bad_old_password")
        perr = password_policy_error(
            new_password, min_length=getattr(settings, "password_min_length", 8)
        )
        if perr:
            raise UserError(perr, 400, "weak_password")
        if row.get("password_hash") and verify_password(new_password, row["password_hash"]):
            raise UserError("新密码不能与原密码相同", 400, "same_password")
        with _lock, self._connect() as c:
            c.execute(
                "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
                (hash_password(new_password), _iso(_now()), user_id),
            )
        return self.revoke_user_sessions(user_id, keep_token=keep_token)

    # ---------------------------------------------------------------- 头像
    _AVATAR_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    AVATAR_MAX_BYTES = 4 * 1024 * 1024

    def set_avatar_bytes(self, user_id: str, data: bytes, suffix: str) -> dict[str, Any]:
        """写入头像文件并更新指针。返回最新 user。"""
        ext = (suffix or "").lower()
        if ext not in self._AVATAR_EXT:
            raise UserError("头像仅支持 PNG / JPG / WEBP / GIF", 400, "bad_avatar_type")
        if not data:
            raise UserError("头像文件为空", 400, "empty_avatar")
        if len(data) > self.AVATAR_MAX_BYTES:
            raise UserError(
                f"头像不能超过 {self.AVATAR_MAX_BYTES // 1024 // 1024} MB",
                400, "avatar_too_large",
            )
        # 校验真的是图片（不信扩展名）——避免把任意文件当头像发布到静态路由上
        try:
            from io import BytesIO

            from PIL import Image

            with Image.open(BytesIO(data)) as im:
                im.verify()
        except Exception as exc:
            raise UserError("文件不是有效图片", 400, "bad_avatar_image") from exc

        name = f"{user_id}_{secrets.token_hex(6)}{ext}"
        path = self.avatar_dir / name
        path.write_bytes(data)

        prev = self.get(user_id) or {}
        with _lock, self._connect() as c:
            c.execute(
                "UPDATE users SET avatar=?, avatar_version=avatar_version+1, "
                "updated_at=? WHERE id=?",
                (name, _iso(_now()), user_id),
            )
        self._remove_avatar_file(prev.get("avatar", ""))
        return self.get(user_id) or {}

    def _remove_avatar_file(self, name: str) -> None:
        if not name or name.startswith("http"):
            return
        # 只删自己目录下的文件（name 来自库，但仍做一次目录约束）
        target = (self.avatar_dir / Path(name).name)
        try:
            if target.parent.resolve() != self.avatar_dir.resolve():
                return
        except Exception:
            return
        purge_file(target)  # 沙箱删除守卫会抛 SystemExit，必须走 safe_fs

    def avatar_path(self, name: str) -> Optional[Path]:
        """头像文件名 → 磁盘路径；不存在或越界返回 None。"""
        if not name or name.startswith("http"):
            return None
        p = self.avatar_dir / Path(name).name
        try:
            if p.parent.resolve() != self.avatar_dir.resolve() or not p.exists():
                return None
        except Exception:
            return None
        return p

    # ---------------------------------------------------------------- 管理
    def set_role(self, user_id: str, role: str, *, actor_id: str = "") -> dict[str, Any]:
        if role not in ROLES:
            raise UserError(f"未知角色 {role!r}", 400, "invalid_role")
        target = self.get(user_id)
        if not target:
            raise UserError("用户不存在", 404, "not_found")
        if user_id == actor_id and role != target.get("role"):
            raise UserError(
                "不能修改自己的角色（避免把自己降权后失去管理入口）", 400,
                "self_role_change",
            )
        if target.get("role") == "admin" and role != "admin":
            if self.count_admins() <= 1:
                raise UserError("这是最后一名管理员，不能降级", 400, "last_admin")
        with _lock, self._connect() as c:
            c.execute("UPDATE users SET role=?, updated_at=? WHERE id=?",
                      (role, _iso(_now()), user_id))
        return self.get(user_id) or {}

    def set_status(self, user_id: str, status: str, *, actor_id: str = "") -> dict[str, Any]:
        if status not in _VALID_STATUS:
            raise UserError(f"未知状态 {status!r}", 400, "invalid_status")
        target = self.get(user_id)
        if not target:
            raise UserError("用户不存在", 404, "not_found")
        if user_id == actor_id and status != STATUS_ACTIVE:
            raise UserError("不能停用自己的账号", 400, "self_disable")
        if status != STATUS_ACTIVE and target.get("role") == "admin":
            if self.count_admins() <= 1:
                raise UserError("这是最后一名启用的管理员，不能停用", 400, "last_admin")
        with _lock, self._connect() as c:
            c.execute("UPDATE users SET status=?, updated_at=? WHERE id=?",
                      (status, _iso(_now()), user_id))
        if status != STATUS_ACTIVE:
            self.revoke_user_sessions(user_id)  # 停用即刻失效，不等 token 过期
        return self.get(user_id) or {}

    def bind_wechat(self, user_id: str, *, openid: str, unionid: str = "") -> dict[str, Any]:
        try:
            with _lock, self._connect() as c:
                c.execute(
                    "UPDATE users SET wechat_openid=?, wechat_unionid=?, updated_at=? "
                    "WHERE id=?",
                    (openid, unionid, _iso(_now()), user_id),
                )
        except sqlite3.IntegrityError as exc:
            raise UserError("该微信已绑定其他账号", 409, "wechat_taken") from exc
        return self.get(user_id) or {}

    def delete_user(self, user_id: str, *, actor_id: str = "") -> None:
        """删除用户（连带会话与头像文件）。"""
        target = self.get(user_id)
        if not target:
            raise UserError("用户不存在", 404, "not_found")
        if user_id == actor_id:
            raise UserError("不能删除自己的账号", 400, "self_delete")
        if target.get("role") == "admin" and self.count_admins() <= 1:
            raise UserError("这是最后一名管理员，不能删除", 400, "last_admin")
        with _lock, self._connect() as c:
            c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM users WHERE id=?", (user_id,))
        self._remove_avatar_file(target.get("avatar", ""))

    # ---------------------------------------------------------------- 初始化
    def ensure_bootstrap_admin(self) -> Optional[dict[str, Any]]:
        """按配置播种一个管理员（幂等）。已存在则不动。"""
        settings = get_settings()
        uname = str(getattr(settings, "user_bootstrap_admin_username", "") or "").strip()
        pwd = str(getattr(settings, "user_bootstrap_admin_password", "") or "")
        if not uname or not pwd:
            return None
        if self.get_by_username(uname):
            return None
        user = self.create_user(
            uname, pwd, display_name="管理员", role="admin",
            email=str(getattr(settings, "user_bootstrap_admin_email", "") or ""),
        )
        logger.warning("已按 USER_BOOTSTRAP_ADMIN_* 播种管理员 %r，请尽快修改密码", uname)
        return user


# --------------------------------------------------------------------------- #
# 单例
# --------------------------------------------------------------------------- #
_store: Optional[UserStore] = None


def get_store() -> UserStore:
    global _store
    if _store is None:
        _store = UserStore()
        try:
            _store.ensure_bootstrap_admin()
            _store.purge_expired()
        except Exception:
            logger.exception("用户库初始化（bootstrap/清理）失败，服务继续但请检查配置")
    return _store


def reset_store() -> None:
    """测试用：丢弃单例（下次 get_store 重新按配置建库）。"""
    global _store
    _store = None


# --------------------------------------------------------------------------- #
# Principal 合成 —— 让 AUTH/01 的数据权限模型直接吃用户身份
# --------------------------------------------------------------------------- #
def user_to_principal(user: dict[str, Any], *, quota_per_min: int = 0):
    """用户 → AUTH/01 的 ``Principal``（角色→权限的唯一展开点仍是 specs.py）。"""
    from .auth import Principal

    return Principal(
        user_id=str(user.get("id") or ""),
        tenant=str(user.get("tenant") or ""),
        roles=[str(user.get("role") or DEFAULT_ROLE)],
        quota_per_min=int(quota_per_min or 0),
    )


def role_matrix() -> list[dict[str, Any]]:
    """角色 → 权限清单（设置页的「权限划分」表直接渲染这个）。"""
    from ..tools.specs import ROLE_PERMISSIONS, ToolPermission

    perm_labels = {
        ToolPermission.READ_METADATA: "读取元数据",
        ToolPermission.READ_KNOWLEDGE: "检索业务知识",
        ToolPermission.READ_DATA: "读取业务数据",
        ToolPermission.COMPUTE: "计算与可视化",
        ToolPermission.GENERATE_ARTIFACT: "生成与交付报告",
    }
    out = []
    for role in ROLES:
        perms = ROLE_PERMISSIONS.get(role, set())
        out.append({
            "role": role,
            "label": ROLE_META[role]["label"],
            "summary": ROLE_META[role]["summary"],
            "rank": ROLE_RANK[role],
            "permissions": [p.value for p in ToolPermission if p in perms],
            "permission_labels": [perm_labels[p] for p in ToolPermission if p in perms],
        })
    return out


def admin_manageable_roles() -> list[str]:
    return list(ROLES)
