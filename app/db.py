"""SQLite 持久化层。

使用 Python 内置 sqlite3，文件位于 app/data/lightbox.db。
所有写操作自动 commit。线程安全：每次操作使用独立连接。
"""
import json
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
                multimodal INTEGER NOT NULL DEFAULT 0,
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
        # 迁移：补充 multimodal 列（是否支持多模态请求）
        if "multimodal" not in cols:
            conn.execute(
                "ALTER TABLE providers ADD COLUMN multimodal "
                "INTEGER NOT NULL DEFAULT 0")
        # 对话页：多会话持久化（会话表 + 会话消息表）。summary 存该会话被压缩出的
        # 「上文摘要」，发送时注入为 system 消息；消息 content/meta 为 JSON 文本。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '新对话',
                summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                meta TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # 迁移：旧库补 chat_sessions / chat_messages 索引（新表已有；此处幂等兜底）
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_session "
            "ON chat_messages(session_id, seq)")
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
        multimodal=bool(row["multimodal"]),
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
                         sort_order, multimodal, min_input_tokens, quota_type,
                         quota_limit, used_calls, used_tokens, last_tokens_per_sec,
                         last_call_at, auto_disabled, disable_reason)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (p.name, p.api_type, p.base_url, p.api_key, p.model,
                     int(p.enabled), p.sort_order, int(p.multimodal),
                     p.min_input_tokens, p.quota_type, p.quota_limit,
                     p.used_calls, p.used_tokens, p.last_tokens_per_sec,
                     p.last_call_at, int(p.auto_disabled), p.disable_reason),
                )
                p.id = cur.lastrowid
            else:
                conn.execute(
                    """
                    UPDATE providers SET
                        name=?, api_type=?, base_url=?, api_key=?, model=?,
                        enabled=?, sort_order=?, multimodal=?, min_input_tokens=?,
                        quota_type=?, quota_limit=?, used_calls=?,
                        used_tokens=?, last_tokens_per_sec=?, last_call_at=?,
                        auto_disabled=?, disable_reason=?
                    WHERE id=?
                    """,
                    (p.name, p.api_type, p.base_url, p.api_key, p.model,
                     int(p.enabled), p.sort_order, int(p.multimodal),
                     p.min_input_tokens, p.quota_type, p.quota_limit,
                     p.used_calls, p.used_tokens, p.last_tokens_per_sec,
                     p.last_call_at, int(p.auto_disabled), p.disable_reason, p.id),
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

    # ------------------------------------------------------------------ #
    # 运行时状态写入（定向 UPDATE，不覆盖用户配置字段）
    #
    # 背景：chat / chat_stream 在调用开始时从 DB 读出 Provider 快照，网络调用
    # 可能持续很久；期间用户可能停用 provider、调整优先级、改密钥等。调用结束
    # 后若用旧快照走全字段 upsert 回写，会把陈旧的 enabled / sort_order /
    # api_key 等覆盖回 DB——表现为「停用的 provider 被自动重新启用」「调整过
    # 的优先级被改回去」。因此网关的运行时路径（记用量、自动关闭、撤销多模态
    # 标记）必须用下面的定向 UPDATE：只写运行时字段，永不碰用户配置字段。
    # 全量 upsert 仅保留给 UI 的创建 / 编辑 / 排序路径。
    # ------------------------------------------------------------------ #
    def record_usage(self, provider_id: int, calls: int, tokens: int,
                     tokens_per_sec: float, call_at: str,
                     auto_disable: bool = False) -> None:
        """累加用量并更新最近速度；auto_disable=True 时同时自动关闭。

        只写用量 / 统计与（必要的）关闭字段；enabled 永不会被置回 1，
        sort_order / 密钥等用户配置不受影响。
        """
        with _connect() as conn:
            if auto_disable:
                conn.execute(
                    "UPDATE providers SET "
                    " used_calls = used_calls + ?, used_tokens = used_tokens + ?,"
                    " last_tokens_per_sec = ?, last_call_at = ?,"
                    " enabled = 0, auto_disabled = 1, disable_reason = 'quota'"
                    " WHERE id = ?",
                    (calls, tokens, tokens_per_sec, call_at, provider_id),
                )
            else:
                conn.execute(
                    "UPDATE providers SET "
                    " used_calls = used_calls + ?, used_tokens = used_tokens + ?,"
                    " last_tokens_per_sec = ?, last_call_at = ?"
                    " WHERE id = ?",
                    (calls, tokens, tokens_per_sec, call_at, provider_id),
                )
            conn.commit()

    def disable_auto(self, provider_id: int, reason: str) -> None:
        """自动关闭 provider（连续失败达阈值等），只写关闭相关字段。"""
        with _connect() as conn:
            conn.execute(
                "UPDATE providers SET enabled = 0, auto_disabled = 1, "
                "disable_reason = ? WHERE id = ?",
                (reason, provider_id),
            )
            conn.commit()

    def set_multimodal(self, provider_id: int, flag: bool) -> None:
        """设置多模态标记（多模态误标自愈用），只写该字段。"""
        with _connect() as conn:
            conn.execute(
                "UPDATE providers SET multimodal = ? WHERE id = ?",
                (int(flag), provider_id),
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


# --------------------------------------------------------------------------- #
# 对话会话（多会话持久化 + 上下文摘要）
# --------------------------------------------------------------------------- #

def _now_text() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ChatStore:
    """「对话」页多会话的持久化。

    - chat_sessions：会话元信息（标题 / 压缩摘要 / 时间）。
    - chat_messages：每会话按 seq 顺序的消息；content 为 JSON（文本 str 或图片
      blocks 列表），meta 为 JSON（_meta / _thinking / _tools / _agent_blocks）。
    """

    # -- 会话 ----------------------------------------------------------- #
    def create_session(self, title: str = "新对话") -> int:
        now = _now_text()
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO chat_sessions(title, created_at, updated_at) "
                "VALUES (?, ?, ?)", (title, now, now))
            conn.commit()
            return int(cur.lastrowid)

    def list_sessions(self) -> List[dict]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, title, summary, created_at, updated_at "
                "FROM chat_sessions ORDER BY updated_at DESC, id DESC").fetchall()
        return [dict(r) for r in rows]

    def rename_session(self, session_id: int, title: str) -> None:
        with _connect() as conn:
            conn.execute("UPDATE chat_sessions SET title = ? WHERE id = ?",
                         (title, session_id))
            conn.commit()

    def delete_session(self, session_id: int) -> None:
        with _connect() as conn:
            conn.execute("DELETE FROM chat_messages WHERE session_id = ?",
                         (session_id,))
            conn.execute("DELETE FROM chat_sessions WHERE id = ?",
                         (session_id,))
            conn.commit()

    def touch(self, session_id: int) -> None:
        now = _now_text()
        with _connect() as conn:
            conn.execute("UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
                         (now, session_id))
            conn.commit()

    def set_summary(self, session_id: int, text: str) -> None:
        with _connect() as conn:
            conn.execute("UPDATE chat_sessions SET summary = ? WHERE id = ?",
                         (text, session_id))
            conn.commit()

    def get_summary(self, session_id: int) -> str:
        with _connect() as conn:
            row = conn.execute(
                "SELECT summary FROM chat_sessions WHERE id = ?",
                (session_id,)).fetchone()
        return row["summary"] if row else ""

    # -- 消息 ----------------------------------------------------------- #
    def max_seq(self, session_id: int) -> int:
        """返回该会话当前最大 seq；无消息返回 0。"""
        with _connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM chat_messages "
                "WHERE session_id = ?", (session_id,)).fetchone()
        return int(row["m"])

    def add_message(self, session_id: int, seq: int, role: str,
                    content, meta: Optional[dict] = None) -> None:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO chat_messages(session_id, seq, role, content, "
                "meta, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, seq, role,
                 json.dumps(content, ensure_ascii=False),
                 json.dumps(meta or {}, ensure_ascii=False),
                 _now_text()))
            conn.commit()

    def load_messages(self, session_id: int) -> List[dict]:
        """还原成 ChatPage 消费的消息 dict 列表。

        每条为 {role, content, time, **meta}——meta 里的 _meta/_thinking/_tools/
        _agent_blocks 平铺到顶层，与 ChatSession.messages 的现有结构一致。
        """
        with _connect() as conn:
            rows = conn.execute(
                "SELECT role, content, meta, created_at FROM chat_messages "
                "WHERE session_id = ? ORDER BY seq ASC", (session_id,)).fetchall()
        out: List[dict] = []
        for r in rows:
            try:
                content = json.loads(r["content"])
            except (ValueError, TypeError):
                content = r["content"]
            # time 还原成数值时间戳：UI 用 `ts - last_ts > 300` 判断时间分隔条且需格式化，
            # 必须给 float（存库为人类可读文本，读回时解析）。
            try:
                import datetime as _dt
                ts = _dt.datetime.strptime(
                    r["created_at"], "%Y-%m-%d %H:%M:%S").timestamp()
            except Exception:
                ts = 0.0
            msg: dict = {"role": r["role"], "content": content, "time": ts}
            try:
                meta = json.loads(r["meta"] or "{}")
            except (ValueError, TypeError):
                meta = {}
            if isinstance(meta, dict):
                msg.update(meta)
            out.append(msg)
        return out

    def delete_range(self, session_id: int, min_seq: int) -> None:
        """删除该会话 seq >= min_seq 的消息（min_seq=0 清空全部）。"""
        with _connect() as conn:
            conn.execute("DELETE FROM chat_messages WHERE session_id = ? AND seq >= ?",
                         (session_id, min_seq))
            conn.commit()

    def count_messages(self, session_id: int) -> int:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM chat_messages WHERE session_id = ?",
                (session_id,)).fetchone()
        return int(row["n"])
