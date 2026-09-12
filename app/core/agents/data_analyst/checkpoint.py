"""Checkpoint persistence for AgentState — replayable, resumable runs.

Each finished (or failed) analysis is snapshotted as JSON under
``data/checkpoints/{session}.json`` so a run can be audited/replayed and a
failed session re-invoked continues from its stored objective (resume semantics
at the session level, backed by the memory layer).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

from ....config import get_settings
from .state import AgentState


def _path(session_id: str) -> Path:
    base = Path(get_settings().checkpoint_dir)
    safe = re.sub(r"[^\w-]", "_", session_id)[:80]
    return base / f"{safe}.json"


def save(state: AgentState) -> Optional[Path]:
    try:
        target = _path(state.session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = state.model_dump(mode="json")
        payload["_checkpoint_ts"] = time.time()
        target.write_text(json.dumps(payload, ensure_ascii=False, default=str),
                          encoding="utf-8")
        return target
    except Exception:
        return None  # checkpoint 失败绝不阻断主流程


def load(session_id: str) -> Optional[AgentState]:
    target = _path(session_id)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload.pop("_checkpoint_ts", None)
        return AgentState.model_validate(payload)
    except Exception:
        return None


def exists(session_id: str) -> bool:
    return _path(session_id).exists()
