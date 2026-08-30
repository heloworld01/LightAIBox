"""SQLite 持久化层。

使用 Python 内置 sqlite3，文件位于 app/data/lightbox.db。
所有写操作自动 commit。线程安全：每次操作使用独立连接。
"""
import os
import sqlite3
from typing import List, Optional

from . import config
from .models import CallLog, Provider


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建目录 + 建表（幂等）。"""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                api_type TEXT NOT NULL,
                base_url TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                min_input_tokens INTEGER NOT NULL DEFAULT 0,
                quota_type TEXT NOT NULL DEFAULT 'unlimited',
                quota_limit INTEGER NOT NULL DEFAULT 0,
                used_calls INTEGER NOT NULL DEFAULT 0,
                used_tokens INTEGER NOT NULL DEFAULT 0,
                last_tokens_per_sec REAL NOT NULL DEFAULT 0,
                last_call_at TEXT NOT NULL DEFAULT '',
                auto_disabled INTEGER NOT NULL DEFAULT 0,
                disable_reason TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS call_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_id INTEGER,
                model TEXT NOT NULL DEFAULT '',
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                elapsed_ms INTEGER NOT NULL DEFAULT 0,
                tokens_per_sec REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'success',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # 迁移：为已存在的库补充 disable_reason 列（记录自动关闭原因）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(providers)")}
        if "disable_reason" not in cols:
            conn.execute(
                "ALTER TABLE providers ADD COLUMN disable_reason "
                "TEXT NOT NULL DEFAULT ''")
        # 迁移：补充 sort_order 列（列表顺序即调度优先级）
        if "sort_order" not in cols:
            conn.execute(
                "ALTER TABLE providers ADD COLUMN sort_order "
                "INTEGER NOT NULL DEFAULT 0")
        # 迁移：补充 min_input_tokens 列（输入字符数门槛，0 表示不限制）
        if "min_input_tokens" not in cols:
            conn.execute(
                "ALTER TABLE providers ADD COLUMN min_input_tokens "
                "INTEGER NOT NULL DEFAULT 0")
        conn.commit()


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #

def _provider_from_row(row: sqlite3.Row) -> Provider:
    return Provider(
        id=row["id"],
        name=row["name"],
        api_type=row["api_type"],
        base_url=row["base_url"],
        api_key=row["api_key"],
        model=row["model"],
        enabled=bool(row["enabled"]),
        sort_order=row["sort_order"],
        min_input_tokens=row["min_input_tokens"],
        quota_type=row["quota_type"],
        quota_limit=row["quota_limit"],
        used_calls=row["used_calls"],
        used_tokens=row["used_tokens"],
        last_tokens_per_sec=row["last_tokens_per_sec"],
        last_call_at=row["last_call_at"],
        auto_disabled=bool(row["auto_disabled"]),
        disable_reason=row["disable_reason"] or "",
    )


class ProviderStore:
    def list(self) -> List[Provider]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM providers ORDER BY sort_order, id").fetchall()
        return [_provider_from_row(r) for r in rows]

    def get(self, provider_id: int) -> Optional[Provider]:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM providers WHERE id = ?", (provider_id,)
            ).fetchone()
        return _provider_from_row(row) if row else None

    def upsert(self, p: Provider) -> int:
        """新增或更新，返回 provider id。"""
        with _connect() as conn:
            if p.id is None:
                cur = conn.execute(
                    """
                    INSERT INTO providers
                        (name, api_type, base_url, api_key, model, enabled,
                         sort_order, min_input_tokens, quota_type, quota_limit,
                         used_calls, used_tokens, last_tokens_per_sec,
                         last_call_at, auto_disabled, disable_reason)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (p.name, p.api_type, p.base_url, p.api_key, p.model,
                     int(p.enabled), p.sort_order, p.min_input_tokens,
                     p.quota_type, p.quota_limit, p.used_calls, p.used_tokens,
                     p.last_tokens_per_sec, p.last_call_at,
                     int(p.auto_disabled), p.disable_reason),
                )
                p.id = cur.lastrowid
            else:
                conn.execute(
                    """
                    UPDATE providers SET
                        name=?, api_type=?, base_url=?, api_key=?, model=?,
                        enabled=?, sort_order=?, min_input_tokens=?,
                        quota_type=?, quota_limit=?, used_calls=?,
                        used_tokens=?, last_tokens_per_sec=?, last_call_at=?,
                        auto_disabled=?, disable_reason=?
                    WHERE id=?
                    """,
                    (p.name, p.api_type, p.base_url, p.api_key, p.model,
                     int(p.enabled), p.sort_order, p.min_input_tokens,
                     p.quota_type, p.quota_limit, p.used_calls, p.used_tokens,
                     p.last_tokens_per_sec, p.last_call_at,
                     int(p.auto_disabled), p.disable_reason, p.id),
                )
            conn.commit()
        return p.id

    def delete(self, provider_id: int) -> None:
        with _connect() as conn:
            conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
            conn.commit()

    def set_enabled(self, provider_id: int, enabled: bool) -> None:
        with _connect() as conn:
            # 手动启用/停用都会清除自动关闭标记与原因
            conn.execute(
                "UPDATE providers SET enabled = ?, auto_disabled = 0, "
                "disable_reason = '' WHERE id = ?",
                (int(enabled), provider_id),
            )
            conn.commit()


# --------------------------------------------------------------------------- #
# CallLog
# --------------------------------------------------------------------------- #

def _log_where(provider_id: Optional[int] = None,
               start: Optional[str] = None,
               end: Optional[str] = None) -> tuple:
    """构造调用记录过滤条件（provider / 时间范围）。

    created_at 为 'YYYY-MM-DD HH:MM:SS' 字符串，可直接按字符串比较。
    返回 (WHERE 子句(或空串), 参数列表)。
    """
    clauses, params = [], []
    if provider_id is not None:
        clauses.append("provider_id = ?")
        params.append(provider_id)
    if start:
        clauses.append("created_at >= ?")
        params.append(start)
    if end:
        clauses.append("created_at <= ?")
        params.append(end)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


class CallLogStore:
    def insert(self, log: CallLog) -> int:
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO call_logs
                    (provider_id, model, prompt_tokens, completion_tokens,
                     total_tokens, elapsed_ms, tokens_per_sec, status, error,
                     created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (log.provider_id, log.model, log.prompt_tokens,
                 log.completion_tokens, log.total_tokens, log.elapsed_ms,
                 log.tokens_per_sec, log.status, log.error, log.created_at),
            )
            log.id = cur.lastrowid
            conn.commit()
        return log.id

    def list(self, limit: int = 200, provider_id: Optional[int] = None,
             start: Optional[str] = None,
             end: Optional[str] = None) -> List[CallLog]:
        """按时间倒序列出调用记录，可按 provider 与时间范围过滤。"""
        where, params = _log_where(provider_id, start, end)
        with _connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM call_logs{where} ORDER BY id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [
            CallLog(
                id=r["id"], provider_id=r["provider_id"], model=r["model"],
                prompt_tokens=r["prompt_tokens"],
                completion_tokens=r["completion_tokens"],
                total_tokens=r["total_tokens"], elapsed_ms=r["elapsed_ms"],
                tokens_per_sec=r["tokens_per_sec"], status=r["status"],
                error=r["error"], created_at=r["created_at"],
            )
            for r in rows
        ]

    def delete(self, log_id: int) -> None:
        """删除单条调用记录。"""
        with _connect() as conn:
            conn.execute("DELETE FROM call_logs WHERE id = ?", (log_id,))
            conn.commit()

    def clear_all(self) -> None:
        """清空全部调用记录。"""
        with _connect() as conn:
            conn.execute("DELETE FROM call_logs")
            conn.commit()

    def stats(self, provider_id: Optional[int] = None,
              start: Optional[str] = None,
              end: Optional[str] = None) -> dict:
        """调用记录汇总统计（按 provider / 时间范围过滤，不受分页限制）。

        返回 {"calls", "successes", "errors",
              "prompt_tokens", "completion_tokens", "total_tokens"}。
        """
        where, params = _log_where(provider_id, start, end)
        with _connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0)      AS total_tokens,
                       COALESCE(SUM(CASE WHEN status = 'success'
                                          THEN 1 ELSE 0 END), 0) AS successes
                FROM call_logs{where}
                """,
                params,
            ).fetchone()
        calls = row["calls"]
        return {
            "calls": calls,
            "successes": row["successes"],
            "errors": calls - row["successes"],
            "prompt_tokens": row["prompt_tokens"],
            "completion_tokens": row["completion_tokens"],
            "total_tokens": row["total_tokens"],
        }
