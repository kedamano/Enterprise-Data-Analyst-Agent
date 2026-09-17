"""AUTH/02 微信扫码登录（微信开放平台「网站应用」）。

⚠️ 先读这段再改代码
------------------
真实扫码登录需要**已认证的微信开放平台网站应用**：企业主体 + 300 元/年认证，
拿到 `appid` / `appsecret`，并在开放平台登记本服务的回调域名。这三样缺一样，
真实链路就走不通——**这是平台门槛，不是代码问题**。

所以本模块的立场是：
- 凭据齐全 → 走**真实** OAuth：state → 回调 code → 换 access_token → 取 userinfo
  → 按 openid/unionid 找人或建号 → 签发本站会话令牌。
- 凭据缺失 → **绝不伪造登录**。`start()` 如实返回 `configured=False`，前端据此
  展示配置指引；只有显式打开 `WECHAT_DEV_SIMULATE=true` 时，才提供一条**明确标注
  `simulated: true`** 的本地模拟通道，用于把 UI 与状态机跑通（不冒充微信授权）。

为什么 state 落 SQLite 而不是内存字典：uvicorn `--workers N` 下每个 worker 一份内存，
扫码请求落在 A worker、轮询落在 B worker 就会永远 pending。这类"单进程测试全过、
多进程必现"的坑不值得埋，20 行 SQLite 就绕开了。

为什么轮询而不是让回调直接回前端：扫码发生在**用户手机上的微信里**，授权后是微信的
浏览器跳到我们的回调地址，拿不到桌面页面的上下文。所以回调只做"落库结果"，
桌面页面轮询取回。
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
import threading
import time
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("da.auth.wechat")

WECHAT_AUTHORIZE_URL = "https://open.weixin.qq.com/connect/qrconnect"
WECHAT_TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
WECHAT_USERINFO_URL = "https://api.weixin.qq.com/sns/userinfo"

STATE_PENDING = "pending"
STATE_CONFIRMED = "confirmed"
STATE_EXPIRED = "expired"
STATE_ERROR = "error"

_lock = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wechat_states (
    state       TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    mode        TEXT NOT NULL DEFAULT 'real',   -- real | simulated
    user_id     TEXT NOT NULL DEFAULT '',
    token       TEXT NOT NULL DEFAULT '',
    expires_in  REAL NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    profile     TEXT NOT NULL DEFAULT ''        -- JSON：昵称/头像等（仅展示用）
);
"""

# 微信错误码 → 人话。只列常见的；未列出的走 `errcode/errmsg` 原样透出。
_WECHAT_ERRORS: dict[int, str] = {
    40029: "授权码无效或已使用（请重新扫码）",
    40030: "授权码已过期，请重新扫码",
    40163: "该授权码已被使用过，请重新扫码",
    41008: "缺少授权码参数",
    42001: "微信 access_token 已过期",
    48001: "该应用没有此接口权限（需已认证的网站应用）",
    40013: "appid 无效",
    40125: "appsecret 无效",
}


class WeChatError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = "wechat_error"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def _now() -> float:
    return time.time()


class WeChatLogin:
    """state 生命周期 + 微信 OAuth 交换。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        from ...config import get_settings

        settings = get_settings()
        self.db = Path(db_path or "data/wechat_login.db")
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with _lock, sqlite3.connect(self.db) as c:
            c.executescript(_SCHEMA)
        self._appid = str(getattr(settings, "wechat_appid", "") or "").strip()
        self._secret = str(getattr(settings, "wechat_appsecret", "") or "").strip()
        self._redirect = str(getattr(settings, "wechat_redirect_uri", "") or "").strip()
        self._ttl = max(60, int(getattr(settings, "wechat_state_ttl_s", 600) or 600))
        self._dev_simulate = bool(getattr(settings, "wechat_dev_simulate", False))

    # ------------------------------------------------------------------ 状态
    @property
    def configured(self) -> bool:
        return bool(self._appid and self._secret and self._redirect)

    @property
    def simulate_available(self) -> bool:
        """未配置凭据 + 显式打开 dev 模拟 → 才提供模拟通道。"""
        return (not self.configured) and self._dev_simulate

    def status(self) -> dict[str, Any]:
        """给前端的能力探测（不含任何密钥）。"""
        missing = [
            name for name, val in (
                ("WECHAT_APPID", self._appid),
                ("WECHAT_APPSECRET", self._secret),
                ("WECHAT_REDIRECT_URI", self._redirect),
            ) if not val
        ]
        return {
            "configured": self.configured,
            "simulate_available": self.simulate_available,
            "missing": missing,
            "redirect_uri": self._redirect,
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def _purge(self, c: sqlite3.Connection) -> None:
        c.execute("DELETE FROM wechat_states WHERE expires_at < ?", (_now() - 3600,))

    def start(self) -> dict[str, Any]:
        """新建一次扫码会话，返回二维码地址与轮询参数。"""
        state = secrets.token_urlsafe(24)
        now = _now()
        with _lock, self._connect() as c:
            self._purge(c)
            c.execute(
                "INSERT INTO wechat_states(state, created_at, expires_at, status, mode) "
                "VALUES(?,?,?,?,?)",
                (state, now, now + self._ttl, STATE_PENDING,
                 "real" if self.configured else "simulated"),
            )

        qr_url = ""
        if self.configured:
            qs = urllib.parse.urlencode({
                "appid": self._appid,
                "redirect_uri": self._redirect,
                "response_type": "code",
                "scope": "snsapi_login",
                "state": state,
            })
            # `#wechat_redirect` 是微信要求的锚点，不能省
            qr_url = f"{WECHAT_AUTHORIZE_URL}?{qs}#wechat_redirect"

        return {
            "state": state,
            "qr_url": qr_url,
            "expires_in": self._ttl,
            "poll_interval_s": 2,
            "configured": self.configured,
            "simulated": not self.configured,
        }

    def _get(self, c: sqlite3.Connection, state: str) -> Optional[sqlite3.Row]:
        if not state:
            return None
        return c.execute(
            "SELECT * FROM wechat_states WHERE state=?", (state,)
        ).fetchone()

    def poll(self, state: str) -> dict[str, Any]:
        """前端轮询：未过期且 pending → pending；已确认 → 带 token。"""
        with _lock, self._connect() as c:
            row = self._get(c, state)
        if row is None:
            return {"status": STATE_EXPIRED, "message": "扫码会话不存在或已过期"}
        if row["status"] == STATE_PENDING and row["expires_at"] < _now():
            with _lock, self._connect() as c:
                c.execute("UPDATE wechat_states SET status=? WHERE state=?",
                          (STATE_EXPIRED, state))
            return {"status": STATE_EXPIRED, "message": "二维码已过期，请刷新"}
        out: dict[str, Any] = {"status": row["status"], "simulated": row["mode"] == "simulated"}
        if row["status"] == STATE_CONFIRMED:
            out.update({
                "token": row["token"],
                "expires_in": row["expires_in"],
                "user_id": row["user_id"],
            })
        elif row["status"] == STATE_ERROR:
            out["message"] = row["error"] or "微信授权失败"
        return out

    def _finish(self, state: str, *, user_id: str, token: str,
                expires_in: float) -> None:
        with _lock, self._connect() as c:
            c.execute(
                "UPDATE wechat_states SET status=?, user_id=?, token=?, expires_in=? "
                "WHERE state=?",
                (STATE_CONFIRMED, user_id, token, expires_in, state),
            )

    def _fail(self, state: str, message: str) -> None:
        with _lock, self._connect() as c:
            c.execute("UPDATE wechat_states SET status=?, error=? WHERE state=?",
                      (STATE_ERROR, message[:300], state))

    # ------------------------------------------------------------------ 真实 OAuth
    def _exchange_code(self, code: str) -> dict[str, Any]:
        """code → {openid, unionid, access_token}。"""
        import httpx

        params = {
            "appid": self._appid,
            "secret": self._secret,
            "code": code,
            "grant_type": "authorization_code",
        }
        try:
            with httpx.Client(timeout=10.0, trust_env=False) as client:
                r = client.get(WECHAT_TOKEN_URL, params=params)
                data = r.json()
        except Exception as exc:
            raise WeChatError(f"无法连接微信开放平台：{exc}", 502, "wechat_unreachable") from exc

        if data.get("errcode"):
            code_num = int(data.get("errcode") or 0)
            msg = _WECHAT_ERRORS.get(code_num) or data.get("errmsg") or "微信授权失败"
            raise WeChatError(f"{msg}（errcode={code_num}）", 400, "wechat_api_error")
        if not data.get("openid"):
            raise WeChatError("微信未返回 openid", 502, "wechat_no_openid")
        return data

    def _fetch_profile(self, access_token: str, openid: str) -> dict[str, Any]:
        """取昵称/头像。失败不算致命——登录本身已成立，资料缺省即可。"""
        import httpx

        try:
            with httpx.Client(timeout=10.0, trust_env=False) as client:
                r = client.get(WECHAT_USERINFO_URL, params={
                    "access_token": access_token, "openid": openid, "lang": "zh_CN",
                })
                data = r.json()
        except Exception:
            return {}
        if data.get("errcode"):
            logger.info("微信 userinfo 未取到（errcode=%s），继续以 openid 登录",
                        data.get("errcode"))
            return {}
        return {
            "nickname": str(data.get("nickname") or ""),
            "headimgurl": str(data.get("headimgurl") or ""),
            "unionid": str(data.get("unionid") or ""),
            "sex": data.get("sex"),
            "country": data.get("country") or "",
            "province": data.get("province") or "",
            "city": data.get("city") or "",
        }

    def handle_callback(self, code: str, state: str) -> dict[str, Any]:
        """微信回调入口：换 token → 取资料 → 找/建用户 → 签发本站会话。"""
        if not self.configured:
            raise WeChatError("微信登录未配置（缺少 appid/secret/redirect_uri）",
                              503, "wechat_not_configured")
        if not state:
            raise WeChatError("缺少 state 参数", 400, "missing_state")
        with _lock, self._connect() as c:
            row = self._get(c, state)
        if row is None:
            raise WeChatError("state 无效（可能已过期，请重新扫码）", 400, "bad_state")
        if row["expires_at"] < _now():
            self._fail(state, "二维码已过期")
            raise WeChatError("二维码已过期，请重新扫码", 400, "state_expired")
        if not code:
            # 用户在微信里点了"取消" → 微信带回 code 缺失
            self._fail(state, "用户取消授权")
            raise WeChatError("已取消微信授权", 400, "user_cancelled")

        try:
            token_data = self._exchange_code(code)
            openid = str(token_data["openid"])
            profile = self._fetch_profile(token_data["access_token"], openid)
            unionid = profile.get("unionid") or str(token_data.get("unionid") or "")
            user, created = self._resolve_user(openid, unionid, profile)
        except WeChatError as exc:
            self._fail(state, exc.message)
            raise
        except Exception as exc:  # noqa: BLE001
            self._fail(state, f"登录失败：{exc}")
            raise WeChatError(f"微信登录失败：{exc}", 500, "wechat_failed") from exc

        from .users import get_store

        sess = get_store().issue_token(user["id"], user_agent="wechat-qr",
                                      ip="wechat")
        get_store().touch_login(user["id"])
        self._finish(state, user_id=user["id"], token=sess["token"],
                     expires_in=float(self._ttl))
        logger.info("微信扫码登录成功 user=%s created=%s", user["username"], created)
        return {"user": user, "created": created}

    def _resolve_user(self, openid: str, unionid: str,
                      profile: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """按 openid/unionid 找人；没有就建一个只能扫码登录的账号。"""
        from .users import STATUS_ACTIVE, UserError, get_store

        store = get_store()
        existing = store.get_by_openid(openid, unionid)
        if existing:
            if existing.get("status") != STATUS_ACTIVE:
                raise WeChatError("该微信绑定的账号已被停用，请联系管理员", 403,
                                  "disabled")
            # 补一次 unionid / 昵称（首次授权可能没带 unionid）
            if unionid and not existing.get("wechat_unionid"):
                try:
                    store.bind_wechat(existing["id"], openid=openid, unionid=unionid)
                except UserError:
                    pass
            return store.get(existing["id"]) or existing, False

        nickname = (profile.get("nickname") or "").strip()
        username = self._generate_username(nickname)
        try:
            user = store.create_user(
                username, "",  # 无密码 → 只能扫码登录（authenticate 会明确提示）
                display_name=nickname or "微信用户",
                source="wechat", wechat_openid=openid, wechat_unionid=unionid,
            )
        except UserError as exc:
            raise WeChatError(f"创建账号失败：{exc.message}", 400, "create_failed") from exc
        return user, True

    @staticmethod
    def _generate_username(nickname: str) -> str:
        """微信昵称可能含 emoji/空格/中文 → 生成一个稳定的合法用户名。"""
        base = "".join(ch for ch in (nickname or "") if ch.isalnum())[:12]
        if len(base) < 3:
            base = "wxuser"
        return f"wx_{base}_{secrets.token_hex(3)}"

    # ------------------------------------------------------------------ 模拟通道
    def simulate(self, state: str, *, nickname: str = "") -> dict[str, Any]:
        """**仅开发**：把一次扫码会话直接置为成功，用于本地跑通 UI 与状态机。

        三重闸门，缺一不可：未配置真实凭据、`WECHAT_DEV_SIMULATE=true`、state 有效。
        产出严格标注 `simulated: true`，前端会显示醒目的"模拟"标记——
        绝不让人误以为这是真的微信授权。
        """
        if self.configured:
            raise WeChatError("已配置真实微信凭据，模拟通道关闭", 400, "simulate_disabled")
        if not self._dev_simulate:
            raise WeChatError(
                "模拟登录未开启。需要真实扫码请配置 WECHAT_APPID / WECHAT_APPSECRET / "
                "WECHAT_REDIRECT_URI；本地联调可临时设 WECHAT_DEV_SIMULATE=true。",
                503, "simulate_disabled",
            )
        with _lock, self._connect() as c:
            row = self._get(c, state)
        if row is None or row["expires_at"] < _now():
            raise WeChatError("扫码会话不存在或已过期", 400, "bad_state")

        name = (nickname or "").strip() or "模拟微信用户"
        # 稳定的合成 openid：同一昵称重复模拟 → 落到同一个账号，便于反复联调
        openid = "devopenid_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
        user, created = self._resolve_user(openid, "", {"nickname": name})

        from .users import get_store

        sess = get_store().issue_token(user["id"], user_agent="wechat-simulate",
                                      ip="127.0.0.1")
        self._finish(state, user_id=user["id"], token=sess["token"],
                     expires_in=float(self._ttl))
        return {"user": user, "created": created, "simulated": True}


# --------------------------------------------------------------------------- #
# 单例
# --------------------------------------------------------------------------- #
_login: Optional[WeChatLogin] = None


def get_login() -> WeChatLogin:
    global _login
    if _login is None:
        _login = WeChatLogin()
    return _login


def reset_login() -> None:
    global _login
    _login = None


def callback_html(ok: bool, message: str) -> str:
    """回调结果页（在微信内置浏览器里展示）。刻意极简、不引外部资源。"""
    color = "#059669" if ok else "#e11d48"
    icon = "✓" if ok else "!"
    hint = "登录已完成，请回到电脑端控制台继续操作。" if ok else "请回到控制台重新扫码。"
    safe_msg = (
        message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>微信登录</title></head>
<body style="margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center;
background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;">
  <div style="max-width:340px;padding:32px 24px;text-align:center;background:#fff;
    border:1px solid #e2e8f0;border-radius:16px;box-shadow:0 1px 3px rgba(15,23,42,.06);">
    <div style="width:52px;height:52px;margin:0 auto 16px;border-radius:50%;
      background:{color}1a;color:{color};display:flex;align-items:center;
      justify-content:center;font-size:26px;font-weight:600;">{icon}</div>
    <p style="margin:0;font-size:15px;font-weight:600;color:#0f172a;">{safe_msg}</p>
    <p style="margin:10px 0 0;font-size:13px;line-height:1.6;color:#64748b;">{hint}</p>
  </div>
</body></html>"""


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
