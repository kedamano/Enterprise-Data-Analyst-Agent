"""Schema migration CLI for EDAA.

Wraps Alembic with project-specific defaults:
  - Reads DATABASE_URL from .env / environment
  - Uses baseline migration (0001) as starting point
  - Idempotent: can be run repeatedly without side effects

Usage:
    python scripts/run_migrations.py revision --autogenerate -m "description"
    python scripts/run_migrations.py upgrade head
    python scripts/run_migrations.py downgrade -1
    python scripts/run_migrations.py current
    python scripts/run_migrations.py history
    python scripts/run_migrations.py verify  # check all DBs are at head
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _load_dotenv() -> None:
    """Load .env into os.environ if present (no dependency)."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=", )
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def main() -> None:
    """Dispatch to Alembic with project config."""
    _load_dotenv()

    import subprocess

    alembic_ini = PROJECT_ROOT / "alembic.ini"
    if not alembic_ini.exists():
        print(f"ERROR: alembic.ini not found at {alembic_ini}", file=sys.stderr)
        sys.exit(1)

    # Allow overriding DATABASE_URL from CLI
    db_url = os.environ.get("DATABASE_URL", "")
    env = os.environ.copy()
    if not db_url:
        # Default to a unified metadata DB
        db_path = PROJECT_ROOT / "data" / "db" / "metadata.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        env["DATABASE_URL"] = f"sqlite:///{db_path}"

    cmd = [sys.executable, "-m", "alembic", "-c", str(alembic_ini)] + sys.argv[1:]
    print(f"[migrate] DATABASE_URL={env['DATABASE_URL']}")
    result = subprocess.run(cmd, env=env, cwd=str(PROJECT_ROOT))
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
