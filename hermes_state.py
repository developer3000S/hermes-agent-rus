#!/usr/bin/env python3
"""
Хранилище состояния SQLite для агента Hermes.

Обеспечивает постоянное хранение сессий с полнотекстовым поиском FTS5, заменяя
подход с использованием JSONL-файлов для каждой сессии. Хранит метаданные сессий,
полную историю сообщений и конфигурацию моделей для сессий CLI и шлюза.

Основные проектные решения:
- Режим WAL для одновременного чтения несколькими читателями и записи одним писателем (шлюз для нескольких платформ)
- Виртуальная таблица FTS5 для быстрого текстового поиска по всем сообщениям сессии
- Разделение сессий, инициируемое сжатием, через цепочки parent_session_id
- Пакетный запуск и траектории RL здесь НЕ хранятся (отдельные системы)
- Тегирование источников сессий ('cli', 'telegram', 'discord' и т. д.) для фильтрации
"""

import json
import logging
import random
import re
import sqlite3
import threading
import time
from pathlib import Path

from agent.memory_manager import sanitize_context
from hermes_constants import get_hermes_home
from typing import Any, Callable, Dict, List, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_DB_PATH = get_hermes_home() / "state.db"

SCHEMA_VERSION = 11

# ---------------------------------------------------------------------------
# Резервный вариант при несовместимости с WAL
# ---------------------------------------------------------------------------
# Режим WAL в SQLite требует координации через общую память (mmap) и блокировок
# диапазонов байтов fcntl, которые не всегда надежно работают на сетевых файловых
# системах (NFS, SMB/CIFS, некоторые монтирования FUSE, WSL1). В документации
# это описано явно: https://www.sqlite.org/wal.html#sometimes_queries_return_sqlite_busy_in_wal_mode
#
# На таких файловых системах ``PRAGMA journal_mode=WAL`` вызывает
# ``sqlite3.OperationalError: locking protocol`` (SQLITE_PROTOCOL). Если мы
# проигнорируем это, все функции, использующие state.db / kanban.db, незаметно
# сломаются — /resume, /title, /history, /branch, диспетчер kanban и т. д.
#
# Вместо этого мы откатываемся к ``journal_mode=DELETE`` (режим по умолчанию
# до появления WAL), который работает на NFS. Параллелизм снижается —
# читатели блокируются во время записи — но функциональность сохраняется.
_WAL_INCOMPAT_MARKERS = (
    "locking protocol",       # SQLITE_PROTOCOL на NFS/SMB
    "not authorized",         # Некоторые монтирования FUSE блокируют pragma WAL напрямую
    "disk i/o error",         # Нестабильная сетевая ФС во время настройки WAL
)

# Ошибка последней инициализации SessionDB() для каждого процесса. Выводится в
# /resume и связанных строках ошибок слэш-команд, чтобы пользователи знали, ПОЧЕМУ
# БД недоступна, вместо получения общего сообщения "Session database not available."
# Только SessionDB.__init__ записывает сюда; ошибки kanban_db.connect() не
# обновляют это (по задумке — ошибки kanban сообщаются через собственную
# обработку ошибок вызывающей стороны).
_last_init_error: Optional[str] = None
_last_init_error_lock = threading.Lock()

# Пути, для которых мы уже вывели ПРЕДУПРЕЖДЕНИЕ об откате к WAL. Без этого
# kanban_db.connect() (вызываемый при каждой операции kanban — см.
# hermes_cli/kanban_db.py для ~30 мест вызова) выводил бы одно и то же
# предупреждение о несовместимости ФС при каждом подключении, засоряя errors.log.
_wal_fallback_warned_paths: set[str] = set()
_wal_fallback_warned_lock = threading.Lock()


def _set_last_init_error(msg: Optional[str]) -> None:
    """Записать (или очистить) информацию о последнем сбое инициализации state.db.

    Потокобезопасно через _last_init_error_lock. Вызывающие стороны передают
    сообщение для записи ошибки или None для очистки. SessionDB.__init__
    вызывает это только для ЗАПИСИ при ошибке — метод намеренно НЕ очищает
    сообщение при успехе, потому что в многопоточной среде (например, шлюз
    или веб-сервер, создающий экземпляры SessionDB() на каждый запрос)
    успешное открытие в одном потоке может стереть причину ошибки, которую
    обработчик /resume в другом потоке как раз собирается вывести. Явная
    очистка (например, в тестовых фикстурах) по-прежнему поддерживается
    при передаче None.
    """
    global _last_init_error
    with _last_init_error_lock:
        _last_init_error = msg


def get_last_init_error() -> Optional[str]:
    """Вернуть информацию о последнем сбое инициализации state.db, если она есть.

    Обработчики слэш-команд (``/resume``, ``/title``, ``/history``, ``/branch``)
    вызывают этот метод, чтобы вывести первопричину в своих сообщениях об
    ошибках, когда ``_session_db is None``. Возвращает ``None``, если
    SessionDB была инициализирована успешно (или попыток не было).
    """
    return _last_init_error


def format_session_db_unavailable(prefix: str = "База данных сессий недоступна") -> str:
    """Сформировать сообщение для пользователя о недоступности БД сессий с указанием причины.

    Когда инициализация ``SessionDB()`` завершается ошибкой, вызывающие стороны
    устанавливают ``_session_db = None``. Ранее несколько слэш-команд (/resume,
    /title, /history, /branch) отвечали просто: ``"Session database not available."``
    без указания ПОЧЕМУ. Этот помощник включает перехваченную причину (обычно
    ``"locking protocol"`` из-за NFS/SMB) и указывает пользователям на
    известного виновника, чтобы они могли исправить это сами.

    Пример вывода:
        База данных сессий недоступна: locking protocol (возможно, state.db
        находится на NFS/SMB — см. https://www.sqlite.org/wal.html).
    """
    cause = get_last_init_error()
    if not cause:
        return f"{prefix}."
    hint = ""
    if any(marker in cause.lower() for marker in _WAL_INCOMPAT_MARKERS):
        hint = " (возможно, state.db находится на NFS/SMB/FUSE — см. https://www.sqlite.org/wal.html)"
    return f"{prefix}: {cause}{hint}."


def apply_wal_with_fallback(
    conn: sqlite3.Connection,
    *,
    db_label: str = "state.db",
) -> str:
    """Установить ``journal_mode=WAL`` для ``conn``, с откатом к DELETE при сбое.

    Возвращает фактически установленный режим журнала (``"wal"`` или ``"delete"``).

    На файловых системах, несовместимых с WAL (NFS, SMB, некоторые FUSE), SQLite
    выдает ``OperationalError("locking protocol")`` при попытке установить WAL.
    Мы откатываемся к режиму DELETE — стандартному до появления WAL, который
    работает на NFS, и выводим одно ПРЕДУПРЕЖДЕНИЕ с объяснением причины.

    ПРЕДУПРЕЖДЕНИЕ дедуплицируется по ``db_label``: повторные подключения к
    одной и той же БД (например, kanban_db.connect(), который вызывается при
    каждой операции kanban) выводят сообщение один раз на процесс, а не на
    каждый вызов. Разные db_labels обрабатываются независимо, поэтому state.db
    и kanban.db получат по одному предупреждению на одном и том же монтировании NFS.

    Используется совместно :class:`SessionDB` и ``hermes_cli.kanban_db.connect``,
    чтобы обе базы данных имели идентичное поведение при откате.
    """
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        return "wal"
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if not any(marker in msg for marker in _WAL_INCOMPAT_MARKERS):
            # Посторонняя OperationalError — не проглатываем ее молча.
            raise
        _log_wal_fallback_once(db_label, exc)
        conn.execute("PRAGMA journal_mode=DELETE")
        return "delete"


def _log_wal_fallback_once(db_label: str, exc: Exception) -> None:
    """Вывести одно ПРЕДУПРЕЖДЕНИЕ на (процесс, db_label) об откате WAL.

    Без этой дедупликации пользователи NFS, работающие с kanban (который открывает
    новое соединение при каждой операции — см. hermes_cli/kanban_db.py),
    забивали бы errors.log сотнями идентичных предупреждений в час.
    """
    with _wal_fallback_warned_lock:
        if db_label in _wal_fallback_warned_paths:
            return
        _wal_fallback_warned_paths.add(db_label)
    logger.warning(
        "%s: Режим журнала WAL не поддерживается на этой файловой системе (%s) — "
        "откат к journal_mode=DELETE (более медленный режим журнала отката; "
        "снижает параллелизм, но работает на NFS/SMB/FUSE). Подробности см. на "
        "https://www.sqlite.org/wal.html. Это предупреждение выводится один "
        "раз на процесс для каждой базы данных.",
        db_label,
        exc,
    )


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    api_call_count INTEGER DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT
);

CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
"""

# Таблица FTS5 с триграммами для поиска подстрок CJK. Токенизатор unicode61 по умолчанию
# разбивает символы CJK на отдельные токены, что нарушает поиск фраз. Токенизатор
# trigram создает перекрывающиеся 3-байтовые последовательности, поэтому поиск
# подстрок работает нативно для любого письма (CJK, тайское и т. д.).
FTS_TRIGRAM_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts_trigram WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_update AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts_trigram WHERE rowid = old.id;
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
"""


class SessionDB:
    """
    Хранилище сессий на базе SQLite с поиском FTS5.

    Потокобезопасно для распространенного паттерна шлюза (несколько потоков чтения,
    один поток записи через режим WAL). Каждый метод открывает собственный курсор.
    """

    # ── Настройка конкуренции при записи ──
    # При наличии нескольких процессов hermes (шлюз + сессии CLI + агенты worktree),
    # использующих одну state.db, конкуренция за блокировку записи WAL вызывает
    # заметные зависания TUI. Встроенный обработчик занятости (busy handler) SQLite
    # использует детерминированный график сна, что вызывает эффект конвоя при
    # высокой нагрузке.
    #
    # Вместо этого мы устанавливаем короткий таймаут SQLite (1 с) и обрабатываем
    # повторные попытки на уровне приложения с рандомизированной задержкой (jitter),
    # что естественным образом распределяет конкурирующих писателей и позволяет
    # избежать эффекта конвоя.
    _WRITE_MAX_RETRIES = 15
    _WRITE_RETRY_MIN_S = 0.020   # 20мс
    _WRITE_RETRY_MAX_S = 0.150   # 150мс
    # Пытаться выполнить PASSIVE чекпоинт WAL каждые N успешных записей.
    _CHECKPOINT_EVERY_N_WRITES = 50

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._write_count = 0
        try:
            self._conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                # Короткий таймаут — повторные попытки на уровне приложения с
                # рандомизированной задержкой обрабатывают конкуренцию вместо
                # ожидания во внутреннем обработчике SQLite до 30 секунд.
                timeout=1.0,
                # Режим автофиксации: стандартный для Python isolation_level=""
                # автоматически запускает транзакции при DML, что конфликтует с
                # нашим явным BEGIN IMMEDIATE. None = мы управляем транзакциями сами.
                isolation_level=None,
            )
            self._conn.row_factory = sqlite3.Row
            apply_wal_with_fallback(self._conn, db_label="state.db")
            self._conn.execute("PRAGMA foreign_keys=ON")

            self._init_schema()
        except Exception as exc:
            # Захватываем причину, чтобы /resume и другие могли вывести, ПОЧЕМУ
            # БД сессий недоступна, вместо общего сообщения "Session database
            # not available." Вызывающие стороны, перехватывающие это исключение,
            # сохраняют существующий путь деградации ``self._session_db = None``.
            #
            # Примечание: мы намеренно НЕ очищаем _last_init_error при успехе
            # (отсутствует ветка else). В многопоточных средах (шлюз, веб-сервер)
            # успешное открытие в одном потоке может стереть причину ошибки,
            # которую обработчик /resume в другом потоке собирается вывести.
            # Тесты, которым нужно сбросить состояние, могут явно вызвать
            # ``hermes_state._set_last_init_error(None)``.
            _set_last_init_error(f"{type(exc).__name__}: {exc}")
            raise

    # ── Основной помощник для записи ──

    def _execute_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Выполнить транзакцию записи с BEGIN IMMEDIATE и повтором с джиттером.

        *fn* получает соединение и должен выполнять операторы INSERT/UPDATE/DELETE.
        Вызывающая сторона НЕ должна вызывать ``commit()`` — это обрабатывается
        здесь после возврата из *fn*.

        BEGIN IMMEDIATE захватывает блокировку записи WAL в начале транзакции
        (а не во время фиксации), поэтому конкуренция за блокировку проявляется
        сразу. Если база данных заблокирована (``database is locked``), мы
        отпускаем блокировку Python, ждем случайное время (20–150 мс) и повторяем
        попытку — это прерывает паттерн конвоя, который создает детерминированная
        задержка SQLite.

        Возвращает то, что возвращает *fn*.
        """
        last_err: Optional[Exception] = None
        for attempt in range(self._WRITE_MAX_RETRIES):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
                # Успех — периодический чекпоинт с минимальными усилиями.
                self._write_count += 1
                if self._write_count % self._CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                return result
            except sqlite3.OperationalError as exc:
                err_msg = str(exc).lower()
                if "locked" in err_msg or "busy" in err_msg:
                    last_err = exc
                    if attempt < self._WRITE_MAX_RETRIES - 1:
                        jitter = random.uniform(
                            self._WRITE_RETRY_MIN_S,
                            self._WRITE_RETRY_MAX_S,
                        )
                        time.sleep(jitter)
                        continue
                # Ошибка, не связанная с блокировкой, или попытки исчерпаны — пробрасываем.
                raise
        # Попытки исчерпаны (обычно до этого не доходит).
        raise last_err or sqlite3.OperationalError(
            "database is locked after max retries"
        )

    def _try_wal_checkpoint(self) -> None:
        """Выполнить чекпоинт PASSIVE WAL (best-effort). Никогда не блокирует и не вызывает исключений.

        Сбрасывает зафиксированные кадры WAL обратно в основной файл БД для всех
        кадров, которые в данный момент не требуются другим соединениям.
        Предотвращает неограниченный рост WAL, когда много процессов поддерживают
        постоянные соединения.
        """
        try:
            with self._lock:
                result = self._conn.execute(
                    "PRAGMA wal_checkpoint(PASSIVE)"
                ).fetchone()
                if result and result[1] > 0:
                    logger.debug(
                        "WAL checkpoint: %d/%d pages checkpointed",
                        result[2], result[1],
                    )
        except Exception:
            pass  # Попытка с минимальными усилиями — никогда не фатально.

    def close(self):
        """Закрыть соединение с базой данных.

        Сначала пытается выполнить чекпоинт PASSIVE WAL, чтобы завершающиеся
        процессы помогали предотвратить неограниченный рост файла WAL.
        """
        with self._lock:
            if self._conn:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except Exception:
                    pass
                self._conn.close()
                self._conn = None

    @staticmethod
    def _parse_schema_columns(schema_sql: str) -> Dict[str, Dict[str, str]]:
        """Извлечь ожидаемые столбцы для каждой таблицы из SCHEMA_SQL.

        Использует базу данных SQLite в памяти для разбора SQL — SQLite сама
        обрабатывает весь синтаксис (выражения DEFAULT с запятыми, встроенные
        REFERENCES, ограничения CHECK и т. д.), поэтому краевых случаев с regex нет.
        Открывается БД в памяти, выполняется DDL схемы, и PRAGMA table_info
        извлекает метаданные столбцов.

        Все, что нужно для добавления столбца — добавить его в SCHEMA_SQL;
        цикл согласования подхватит его автоматически.
        """
        ref = sqlite3.connect(":memory:")
        try:
            ref.executescript(schema_sql)
            table_columns: Dict[str, Dict[str, str]] = {}
            for (tbl,) in ref.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall():
                cols: Dict[str, str] = {}
                for row in ref.execute(
                    f'PRAGMA table_info("{tbl}")'
                ).fetchall():
                    # row: (cid, name, type, notnull, dflt_value, pk)
                    col_name = row[1]
                    col_type = row[2] or ""
                    notnull = row[3]
                    default = row[4]
                    pk = row[5]
                    # Reconstruct the type expression for ALTER TABLE ADD COLUMN
                    parts = [col_type] if col_type else []
                    if notnull and not pk:
                        parts.append("NOT NULL")
                    if default is not None:
                        parts.append(f"DEFAULT {default}")
                    cols[col_name] = " ".join(parts)
                table_columns[tbl] = cols
            return table_columns
        finally:
            ref.close()

    def _reconcile_columns(self, cursor: sqlite3.Cursor) -> None:
        """Убедиться, что в активных таблицах есть все столбцы, объявленные в SCHEMA_SQL.

        Следует паттерну Beets/sqlite-utils: определение CREATE TABLE в
        SCHEMA_SQL является единственным источником истины для желаемой схемы.
        При каждом запуске этот метод сравнивает текущие столбцы (через PRAGMA
        table_info) с объявленными столбцами и добавляет (ADD) недостающие.

        Это делает добавление столбцов декларативной операцией — достаточно
        добавить столбец в SCHEMA_SQL, и он появится при следующем запуске.
        Блоки миграции с привязкой к версии больше не нужны для ADD COLUMN.
        """
        expected = self._parse_schema_columns(SCHEMA_SQL)
        for table_name, declared_cols in expected.items():
            # Получить текущие столбцы из активной таблицы
            try:
                rows = cursor.execute(
                    f'PRAGMA table_info("{table_name}")'
                ).fetchall()
            except sqlite3.OperationalError:
                continue  # Таблица еще не существует (не должно происходить после executescript)
            live_cols = set()
            for row in rows:
                # PRAGMA table_info возвращает (cid, name, type, notnull, dflt_value, pk)
                name = row[1] if isinstance(row, (tuple, list)) else row["name"]
                live_cols.add(name)

            for col_name, col_type in declared_cols.items():
                if col_name not in live_cols:
                    safe_name = col_name.replace('"', '""')
                    try:
                        cursor.execute(
                            f'ALTER TABLE "{table_name}" ADD COLUMN "{safe_name}" {col_type}'
                        )
                    except sqlite3.OperationalError as exc:
                        # Ожидаемо: "duplicate column name" при гонке или повторном запуске.
                        # Неожидаемо: "Cannot add a NOT NULL column with default value NULL"
                        # из-за ошибки в схеме. Логируем на уровне DEBUG, чтобы было видно в agent.log.
                        logger.debug(
                            "reconcile %s.%s: %s", table_name, col_name, exc,
                        )

    def _init_schema(self):
        """Создать таблицы и FTS, если они не существуют, согласовать столбцы.

        Управление схемой следует паттерну декларативного согласования (Beets,
        sqlite-utils): SCHEMA_SQL — единственный источник истины. На существующих
        базах данных _reconcile_columns() сравнивает текущие столбцы с SCHEMA_SQL
        и добавляет недостающие. Это устраняет цепочку миграций с контролем версий
        для добавления столбцов, что исключает пропуск столбцов при изменении
        порядка миграций.

        Таблица schema_version сохранена для будущих миграций данных
        (преобразование существующих строк), которые невозможно обработать декларативно.
        """
        cursor = self._conn.cursor()

        cursor.executescript(SCHEMA_SQL)

        # ── Декларативное согласование столбцов ────────────────────────
        # Сравнить активные таблицы с SCHEMA_SQL и добавить недостающие столбцы.
        # Это идемпотентно и самовосстанавливаемо: даже если миграция с контролем
        # версии была пропущена (например, из-за изменения нумерации версий),
        # столбец будет создан здесь.
        self._reconcile_columns(cursor)

        # ── Учет версии схемы ──────────────────────────────────────────
        # Обновить до текущей, чтобы будущие миграции данных могли опираться на
        # версию. Добавление столбцов больше не требует версионных миграций.
        cursor.execute("SELECT version FROM schema_version LIMIT 1")
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
        else:
            current_version = row["version"] if isinstance(row, sqlite3.Row) else row[0]
            # Миграции данных, которые нельзя выразить декларативно (заполнение строк,
            # изменение индексов, привязанное к конкретному шагу версии), остаются
            # в цепочке версий. Добавление столбцов обрабатывается
            # методом _reconcile_columns() выше и больше не требует записей здесь.
            if current_version < 10:
                # v10: таблица FTS5 trigram для поиска CJK/подстрок. Виртуальная
                # таблица и триггеры создаются безусловно через FTS_TRIGRAM_SQL ниже,
                # но существующие строки требуют разового заполнения индекса FTS.
                try:
                    cursor.execute("SELECT * FROM messages_fts_trigram LIMIT 0")
                    _fts_trigram_exists = True
                except sqlite3.OperationalError:
                    _fts_trigram_exists = False
                if not _fts_trigram_exists:
                    cursor.executescript(FTS_TRIGRAM_SQL)
                    cursor.execute(
                        "INSERT INTO messages_fts_trigram(rowid, content) "
                        "SELECT id, content FROM messages WHERE content IS NOT NULL"
                    )
            if current_version < 11:
                # v11: переиндексация таблиц FTS5 для включения tool_name + tool_calls и
                # переход от режима внешнего контента к встроенному. В старых БД есть
                # таблицы FTS и триггеры старой схемы, которые IF NOT EXISTS не
                # перезапишет, поэтому мы явно удаляем их и позволяем проверкам
                # существования (ниже) воссоздать их из FTS_SQL / FTS_TRIGRAM_SQL,
                # а затем заполняем каждую строку сообщения. Исправляет #16751.
                for _trig in (
                    "messages_fts_insert",
                    "messages_fts_delete",
                    "messages_fts_update",
                    "messages_fts_trigram_insert",
                    "messages_fts_trigram_delete",
                    "messages_fts_trigram_update",
                ):
                    try:
                        cursor.execute(f"DROP TRIGGER IF EXISTS {_trig}")
                    except sqlite3.OperationalError:
                        pass
                for _tbl in ("messages_fts", "messages_fts_trigram"):
                    try:
                        cursor.execute(f"DROP TABLE IF EXISTS {_tbl}")
                    except sqlite3.OperationalError:
                        pass
                # Пересоздать виртуальные таблицы и триггеры с новой схемой
                # встроенного режима, которая индексирует content || tool_name || tool_calls.
                cursor.executescript(FTS_SQL)
                cursor.executescript(FTS_TRIGRAM_SQL)
                # Заполнить оба индекса из всех существующих строк сообщений.
                cursor.execute(
                    "INSERT INTO messages_fts(rowid, content) "
                    "SELECT id, "
                    "COALESCE(content, '') || ' ' || "
                    "COALESCE(tool_name, '') || ' ' || "
                    "COALESCE(tool_calls, '') "
                    "FROM messages"
                )
                cursor.execute(
                    "INSERT INTO messages_fts_trigram(rowid, content) "
                    "SELECT id, "
                    "COALESCE(content, '') || ' ' || "
                    "COALESCE(tool_name, '') || ' ' || "
                    "COALESCE(tool_calls, '') "
                    "FROM messages"
                )
            if current_version < SCHEMA_VERSION:
                cursor.execute(
                    "UPDATE schema_version SET version = ?",
                    (SCHEMA_VERSION,),
                )

        # Уникальный индекс заголовка — всегда гарантируем его существование
        try:
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique "
                "ON sessions(title) WHERE title IS NOT NULL"
            )
        except sqlite3.OperationalError:
            pass  # Индекс уже существует

        # Настройка FTS5 (отдельно, так как CREATE VIRTUAL TABLE с IF NOT EXISTS может вести себя нестабильно в executescript)
        try:
            cursor.execute("SELECT * FROM messages_fts LIMIT 0")
        except sqlite3.OperationalError:
            cursor.executescript(FTS_SQL)

        # Trigram FTS5 для поиска CJK/подстрок
        try:
            cursor.execute("SELECT * FROM messages_fts_trigram LIMIT 0")
        except sqlite3.OperationalError:
            cursor.executescript(FTS_TRIGRAM_SQL)

        self._conn.commit()

    # =========================================================================
    # Жизненный цикл сессии
    # =========================================================================

    def _insert_session_row(
        self,
        session_id: str,
        source: str,
        model: str = None,
        model_config: Dict[str, Any] = None,
        system_prompt: str = None,
        user_id: str = None,
        parent_session_id: str = None,
    ) -> None:
        """Общий INSERT OR IGNORE для строк сессий."""
        def _do(conn):
            conn.execute(
                """INSERT OR IGNORE INTO sessions (id, source, user_id, model, model_config,
                   system_prompt, parent_session_id, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    source,
                    user_id,
                    model,
                    json.dumps(model_config) if model_config else None,
                    system_prompt,
                    parent_session_id,
                    time.time(),
                ),
            )
        self._execute_write(_do)

    def create_session(self, session_id: str, source: str, **kwargs) -> str:
        """Создать новую запись сессии. Возвращает session_id."""
        self._insert_session_row(session_id, source, **kwargs)
        return session_id
    def end_session(self, session_id: str, end_reason: str) -> None:
        """Отметить сессию как завершенную.

        Ничего не делает, если сессия уже завершена. Побеждает первый end_reason:
        сессии, разделенные из-за сжатия, должны сохранять запись ``end_reason = 'compression'``,
        даже если позже поступит запоздалый вызов ``end_session()`` (например,
        от десинхронизированного CLI session_id после ``/resume`` или ``/branch``)
        с другой причиной. Сначала используйте ``reopen_session()``, если вам
        намеренно нужно повторно завершить закрытую сессию с новой причиной.
        """
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = ? "
                "WHERE id = ? AND ended_at IS NULL",
                (time.time(), end_reason, session_id),
            )
        self._execute_write(_do)

    def reopen_session(self, session_id: str) -> None:
        """Очистить ended_at/end_reason, чтобы сессию можно было возобновить."""
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?",
                (session_id,),
            )
        self._execute_write(_do)

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        """Сохранить снимок полной собранной системной подсказки (system prompt)."""
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET system_prompt = ? WHERE id = ?",
                (system_prompt, session_id),
            )
        self._execute_write(_do)

    def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: Optional[float] = None,
        actual_cost_usd: Optional[float] = None,
        cost_status: Optional[str] = None,
        cost_source: Optional[str] = None,
        pricing_version: Optional[str] = None,
        billing_provider: Optional[str] = None,
        billing_base_url: Optional[str] = None,
        billing_mode: Optional[str] = None,
        api_call_count: int = 0,
        absolute: bool = False,
    ) -> None:
        """Обновить счетчики токенов и заполнить модель, если она еще не установлена.

        Когда *absolute* имеет значение False (по умолчанию), значения
        **увеличиваются** — используйте это для дельт за каждый вызов API (путь CLI).

        Когда *absolute* имеет значение True, значения **устанавливаются напрямую** —
        используйте это, когда вызывающая сторона уже имеет накопленные итоги
        (путь шлюза, где кэшированный агент накапливает данные по сообщениям).
        """
        # Убеждаемся, что строка сессии существует, чтобы UPDATE не затронул
        # 0 строк. При конкурентной нагрузке (cron + kanban + delegate_task)
        # начальный create_session() мог не сработать из-за блокировки SQLite.
        # INSERT OR IGNORE дешев и идемпотентен.
        self._insert_session_row(session_id, "unknown", model=model)
        if absolute:
            sql = """UPDATE sessions SET
                   input_tokens = ?,
                   output_tokens = ?,
                   cache_read_tokens = ?,
                   cache_write_tokens = ?,
                   reasoning_tokens = ?,
                   estimated_cost_usd = COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?),
                   api_call_count = ?
                   WHERE id = ?"""
        else:
            sql = """UPDATE sessions SET
                   input_tokens = input_tokens + ?,
                   output_tokens = output_tokens + ?,
                   cache_read_tokens = cache_read_tokens + ?,
                   cache_write_tokens = cache_write_tokens + ?,
                   reasoning_tokens = reasoning_tokens + ?,
                   estimated_cost_usd = COALESCE(estimated_cost_usd, 0) + COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE COALESCE(actual_cost_usd, 0) + ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?),
                   api_call_count = COALESCE(api_call_count, 0) + ?
                   WHERE id = ?"""
        params = (
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
            reasoning_tokens,
            estimated_cost_usd,
            actual_cost_usd,
            actual_cost_usd,
            cost_status,
            cost_source,
            pricing_version,
            billing_provider,
            billing_base_url,
            billing_mode,
            model,
            api_call_count,
            session_id,
        )
        def _do(conn):
            conn.execute(sql, params)
        self._execute_write(_do)

    def ensure_session(
        self,
        session_id: str,
        source: str = "unknown",
        model: str = None,
        **kwargs,
    ) -> str:
        """Убедиться, что строка сессии существует (INSERT OR IGNORE). Принимает дополнительные аргументы."""
        self._insert_session_row(session_id, source, model=model, **kwargs)
        return session_id

    def prune_empty_ghost_sessions(self, sessions_dir: "Optional[Path]" = None) -> int:
        """Удалить пустые фантомные сессии TUI (без сообщений, без заголовка, старше 24 часов)."""
        cutoff = time.time() - 86400  # Только сессии старше 24 часов

        def _do(conn):
            rows = conn.execute("""
                SELECT id FROM sessions
                WHERE source = 'tui'
                  AND title IS NULL
                  AND ended_at IS NOT NULL
                  AND started_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM messages WHERE messages.session_id = sessions.id
                  )
            """, (cutoff,)).fetchall()
            ids = [r[0] if isinstance(r, (tuple, list)) else r["id"] for r in rows]
            if ids:
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"DELETE FROM sessions WHERE id IN ({placeholders})", ids
                )
            return ids

        removed_ids = self._execute_write(_do) or []
        # Очистить файлы сессий на диске (для дополнительной надежности)
        if sessions_dir and removed_ids:
            for sid in removed_ids:
                self._remove_session_files(sessions_dir, sid)
        return len(removed_ids)

    def finalize_orphaned_compression_sessions(self) -> int:
        """Отметить осиротевшие сессии продолжения сжатия как завершенные.

        Находит дочерние сессии, которые никогда не были завершены: родитель
        завершен с причиной reason='compression', у ребенка есть сообщения,
        но нет end_reason/ended_at и api_call_count=0. Не разрушительно:
        сохраняет все сообщения и устанавливает end_reason='orphaned_compression'.
        Исправление для #20001.
        """
        cutoff = time.time() - 604800  # 7 дней

        def _do(conn):
            now = time.time()
            result = conn.execute(
                """
                UPDATE sessions
                SET ended_at = ?,
                    end_reason = 'orphaned_compression'
                WHERE api_call_count = 0
                  AND end_reason IS NULL
                  AND ended_at IS NULL
                  AND started_at < ?
                  AND parent_session_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM sessions p
                      WHERE p.id = sessions.parent_session_id
                        AND p.end_reason = 'compression'
                        AND p.ended_at IS NOT NULL
                  )
                  AND EXISTS (
                      SELECT 1 FROM messages m
                      WHERE m.session_id = sessions.id
                  )
                """,
                (now, cutoff),
            )
            return result.rowcount

        return self._execute_write(_do) or 0

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Получить сессию по ID."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def resolve_session_id(self, session_id_or_prefix: str) -> Optional[str]:
        """Преобразовать точный ID сессии или уникальный префикс в полный ID.

        Возвращает точный ID, если он существует. В противном случае рассматривает
        ввод как префикс и возвращает единственный соответствующий ID сессии,
        если префикс однозначен. Возвращает None при отсутствии совпадений или
        неоднозначности префикса.
        """
        exact = self.get_session(session_id_or_prefix)
        if exact:
            return exact["id"]

        escaped = (
            session_id_or_prefix
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\' ORDER BY started_at DESC LIMIT 2",
                (f"{escaped}%",),
            )
            matches = [row["id"] for row in cursor.fetchall()]
        if len(matches) == 1:
            return matches[0]
        return None

    # Maximum length for session titles
    MAX_TITLE_LENGTH = 100

    @staticmethod
    def sanitize_title(title: Optional[str]) -> Optional[str]:
        """Проверить и очистить заголовок сессии.

        - Удаляет начальные/конечные пробелы.
        - Удаляет управляющие символы ASCII (0x00-0x1F, 0x7F) и проблемные
          управляющие символы Unicode (нулевой ширины, переопределения RTL/LTR и т. д.).
        - Сворачивает внутренние последовательности пробелов в одиночные пробелы.
        - Преобразует пустые строки или строки только из пробелов в None.
        - Обеспечивает соблюдение MAX_TITLE_LENGTH.

        Возвращает очищенную строку заголовка или None.
        Вызывает ValueError, если заголовок превышает MAX_TITLE_LENGTH после очистки.
        """
        if not title:
            return None

        # Удаляем управляющие символы ASCII (0x00-0x1F, 0x7F), но сохраняем
        # символы пробела (\t=0x09, \n=0x0A, \r=0x0D), чтобы их можно было
        # нормализовать в пробелы на этапе сворачивания пробелов ниже.
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', title)

        # Удаляем проблемные управляющие символы Unicode:
        # - Символы нулевой ширины (U+200B-U+200F, U+FEFF)
        # - Направленные переопределения (U+202A-U+202E, U+2066-U+2069)
        # - Замену объектов (U+FFFC), межстрочные аннотации (U+FFF9-U+FFFB)
        cleaned = re.sub(
            r'[\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ufffc\ufff9-\ufffb]',
            '', cleaned,
        )

        # Сворачиваем внутренние последовательности пробелов и обрезаем края
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()

        if not cleaned:
            return None

        if len(cleaned) > SessionDB.MAX_TITLE_LENGTH:
            raise ValueError(
                f"Заголовок слишком длинный ({len(cleaned)} симв., макс. {SessionDB.MAX_TITLE_LENGTH})"
            )

        return cleaned

    def set_session_title(self, session_id: str, title: str) -> bool:
        """Установить или обновить заголовок сессии.

        Возвращает True, если сессия найдена и заголовок установлен.
        Вызывает ValueError, если заголовок уже используется другой сессией или
        если заголовок не прошел валидацию (слишком длинный, недопустимые символы).
        Пустые строки или строки только из пробелов нормализуются в None (очистка заголовка).
        """
        title = self.sanitize_title(title)
        def _do(conn):
            if title:
                # Проверка уникальности (разрешаем сессии сохранить собственный заголовок)
                cursor = conn.execute(
                    "SELECT id FROM sessions WHERE title = ? AND id != ?",
                    (title, session_id),
                )
                conflict = cursor.fetchone()
                if conflict:
                    raise ValueError(
                        f"Заголовок '{title}' уже используется сессией {conflict['id']}"
                    )
            cursor = conn.execute(
                "UPDATE sessions SET title = ? WHERE id = ?",
                (title, session_id),
            )
            return cursor.rowcount
        rowcount = self._execute_write(_do)
        return rowcount > 0

    def get_session_title(self, session_id: str) -> Optional[str]:
        """Получить заголовок сессии или None."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return row["title"] if row else None

    def get_session_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        """Найти сессию по точному заголовку. Возвращает словарь сессии или None."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE title = ?", (title,)
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def resolve_session_by_title(self, title: str) -> Optional[str]:
        """Преобразовать заголовок в ID сессии, предпочитая последний в родословной.

        Если существует точное совпадение заголовка, возвращает ID этой сессии.
        Если нет, ищет варианты "заголовок #N" и возвращает последний из них.
        Если существует и точный заголовок, и нумерованные варианты, возвращает
        последний нумерованный вариант (самое свежее продолжение).
        """
        # Сначала пробуем точное совпадение
        exact = self.get_session_by_title(title)

        # Также ищем нумерованные варианты: "заголовок #2", "заголовок #3" и т. д.
        # Экранируем подстановочные знаки SQL LIKE (%, _) в заголовке
        escaped = title.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id, title, started_at FROM sessions "
                "WHERE title LIKE ? ESCAPE '\\' ORDER BY started_at DESC",
                (f"{escaped} #%",),
            )
            numbered = cursor.fetchall()

        if numbered:
            # Возвращаем самый свежий нумерованный вариант
            return numbered[0]["id"]
        elif exact:
            return exact["id"]
        return None

    def get_next_title_in_lineage(self, base_title: str) -> str:
        """Сгенерировать следующий заголовок в родословной (например, "моя сессия" → "моя сессия #2").

        Обрезает существующий суффикс " #N", чтобы найти базовое имя, затем находит
        наибольшее существующее число и увеличивает его.
        """
        # Обрезаем существующий суффикс #N, чтобы найти истинную базу
        match = re.match(r'^(.*?) #(\d+)$', base_title)
        if match:
            base = match.group(1)
        else:
            base = base_title

        # Находим все существующие нумерованные варианты
        # Экранируем подстановочные знаки SQL LIKE (%, _) в базе
        escaped = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE title = ? OR title LIKE ? ESCAPE '\\'",
                (base, f"{escaped} #%"),
            )
            existing = [row["title"] for row in cursor.fetchall()]

        if not existing:
            return base  # Нет конфликта, используем базовое имя как есть

        # Находим наибольшее число
        max_num = 1  # Оригинал без номера считается за #1
        for t in existing:
            m = re.match(r'^.* #(\d+)$', t)
            if m:
                max_num = max(max_num, int(m.group(1)))

        return f"{base} #{max_num + 1}"

    def get_compression_tip(self, session_id: str) -> Optional[str]:
        """Пройти по цепочке продолжений сжатия вперед и вернуть последнюю (tip).

        Продолжение сжатия — это дочерняя сессия, где:
        1. У родителя ``end_reason = 'compression'``.
        2. Дочерняя сессия была создана ПОСЛЕ завершения родительской (started_at >= ended_at).

        Второе условие отличает продолжения сжатия от дочерних сессий делегированных
        агентов или ответвлений (branches), которые также могут иметь
        ``parent_session_id``, но были созданы, пока родитель еще был активен.

        Возвращает session_id последнего продолжения в цепочке или исходный
        ``session_id``, если он не является частью цепочки сжатия (или если сам не существует).
        """
        current = session_id
        # Ограничиваем обход для защиты — цепочки сжатия такой глубины патологичны
        # и не должны встречаться на практике. 100 = более чем достаточно.
        for _ in range(100):
            with self._lock:
                cursor = self._conn.execute(
                    "SELECT id FROM sessions "
                    "WHERE parent_session_id = ? "
                    "  AND started_at >= ("
                    "      SELECT ended_at FROM sessions "
                    "      WHERE id = ? AND end_reason = 'compression'"
                    "  ) "
                    "ORDER BY started_at DESC LIMIT 1",
                    (current, current),
                )
                row = cursor.fetchone()
            if row is None:
                return current
            current = row["id"]
        return current

    def list_sessions_rich(
        self,
        source: str = None,
        exclude_sources: List[str] = None,
        limit: int = 20,
        offset: int = 0,
        include_children: bool = False,
        project_compression_tips: bool = True,
        order_by_last_active: bool = False,
    ) -> List[Dict[str, Any]]:
        """Список сессий с предпросмотром (первое сообщение пользователя) и временем последней активности.

        Возвращает словари с ключами: id, source, model, title, started_at, ended_at,
        message_count, preview (первые 60 символов первого сообщения пользователя),
        last_active (временная метка последнего сообщения).

        Использует один запрос с коррелированными подзапросами вместо N+2 запросов.

        По умолчанию дочерние сессии (запуски субагентов, продолжения сжатия)
        исключаются. Передайте ``include_children=True``, чтобы включить их.

        При ``project_compression_tips=True`` (по умолчанию) сессии, являющиеся
        корнями цепочек сжатия, проецируются вперед на их последнее продолжение —
        один логический разговор = одна запись в списке, показывающая id,
        message_count, заголовок и last_active активного продолжения. Это
        предотвращает невидимость сжатых продолжений для пользователей, сохраняя
        при этом субагентов и ветки скрытыми. Передайте ``False``, чтобы вернуть
        сырые корневые строки (полезно для интерфейсов администрирования/отладки).

        Передайте ``order_by_last_active=True`` для сортировки по самой недавней
        активности вместо времени начала разговора. Для цепочек сжатия "самая
        недавняя активность" берется из наконечника (tip), а не из корня, поэтому
        старый разговор, который был сжат и продолжен недавно, окажется на
        нужном месте. Сортировка вычисляется на уровне SQL через рекурсивное CTE,
        которое проходит по ребрам продолжения сжатия, поэтому LIMIT и OFFSET
        по-прежнему работают эффективно.
        """
        where_clauses = []
        params = []

        if not include_children:
            # Show root sessions and branch sessions (whose parent ended with
            # end_reason='branched' before the child was created), while still
            # hiding sub-agent runs and compression continuations (which also
            # carry a parent_session_id but were spawned while the parent was
            # still live — i.e., started_at < parent.ended_at).
            where_clauses.append(
                "(s.parent_session_id IS NULL"
                " OR EXISTS (SELECT 1 FROM sessions p"
                "            WHERE p.id = s.parent_session_id"
                "            AND p.end_reason = 'branched'"
                "            AND s.started_at >= p.ended_at))"
            )

        if source:
            where_clauses.append("s.source = ?")
            params.append(source)
        if exclude_sources:
            placeholders = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({placeholders})")
            params.extend(exclude_sources)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        if order_by_last_active:
            # Compute effective_last_active by walking each surfaced session's
            # compression-continuation chain forward in SQL and taking the MAX
            # timestamp across the chain. This lets us ORDER BY + LIMIT at SQL
            # level instead of fetching every row and sorting in Python, while
            # still surfacing old compression roots whose live tip is fresh.
            #
            # The CTE seeds from rows the outer WHERE admits (roots + branch
            # children), then recursively joins forward through
            # compression-continuation edges using the same criteria as
            # get_compression_tip (parent.end_reason='compression' AND
            # child.started_at >= parent.ended_at).
            query = f"""
                WITH RECURSIVE chain(root_id, cur_id) AS (
                    SELECT s.id, s.id FROM sessions s {where_sql}
                    UNION ALL
                    SELECT c.root_id, child.id
                    FROM chain c
                    JOIN sessions parent ON parent.id = c.cur_id
                    JOIN sessions child ON child.parent_session_id = c.cur_id
                    WHERE parent.end_reason = 'compression'
                      AND child.started_at >= parent.ended_at
                ),
                chain_max AS (
                    SELECT
                        root_id,
                        MAX(COALESCE(
                            (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = cur_id),
                            (SELECT started_at FROM sessions ss WHERE ss.id = cur_id)
                        )) AS effective_last_active
                    FROM chain
                    GROUP BY root_id
                )
                SELECT s.*,
                    COALESCE(
                        (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                         FROM messages m
                         WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                         ORDER BY m.timestamp, m.id LIMIT 1),
                        ''
                    ) AS _preview_raw,
                    COALESCE(
                        (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                        s.started_at
                    ) AS last_active,
                    COALESCE(cm.effective_last_active, s.started_at) AS _effective_last_active
                FROM sessions s
                LEFT JOIN chain_max cm ON cm.root_id = s.id
                {where_sql}
                ORDER BY _effective_last_active DESC, s.started_at DESC, s.id DESC
                LIMIT ? OFFSET ?
            """
            # WHERE params apply twice (CTE seed + outer select).
            params = params + params + [limit, offset]
        else:
            query = f"""
                SELECT s.*,
                    COALESCE(
                        (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                         FROM messages m
                         WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                         ORDER BY m.timestamp, m.id LIMIT 1),
                        ''
                    ) AS _preview_raw,
                    COALESCE(
                        (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                        s.started_at
                    ) AS last_active
                FROM sessions s
                {where_sql}
                ORDER BY s.started_at DESC
                LIMIT ? OFFSET ?
            """
            params.extend([limit, offset])
        with self._lock:
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()
        sessions = []
        for row in rows:
            s = dict(row)
            # Build the preview from the raw substring
            raw = s.pop("_preview_raw", "").strip()
            if raw:
                text = raw[:60]
                s["preview"] = text + ("..." if len(raw) > 60 else "")
            else:
                s["preview"] = ""
            # Drop the internal ordering column so callers see a clean dict.
            s.pop("_effective_last_active", None)
            sessions.append(s)

        # Project compression roots forward to their tips. Each row whose
        # end_reason is 'compression' has a continuation child; replace the
        # surfaced fields (id, message_count, title, last_active, ended_at,
        # end_reason, preview) with the tip's values so the list entry acts
        # as the live conversation. Keep the root's started_at to preserve
        # chronological ordering by original conversation start.
        if project_compression_tips and not include_children:
            projected = []
            for s in sessions:
                if s.get("end_reason") != "compression":
                    projected.append(s)
                    continue
                tip_id = self.get_compression_tip(s["id"])
                if tip_id == s["id"]:
                    projected.append(s)
                    continue
                tip_row = self._get_session_rich_row(tip_id)
                if not tip_row:
                    projected.append(s)
                    continue
                # Preserve the root's started_at for stable sort order, but
                # surface the tip's identity and activity data.
                merged = dict(s)
                for key in (
                    "id", "ended_at", "end_reason", "message_count",
                    "tool_call_count", "title", "last_active", "preview",
                    "model", "system_prompt",
                ):
                    if key in tip_row:
                        merged[key] = tip_row[key]
                merged["_lineage_root_id"] = s["id"]
                projected.append(merged)
            sessions = projected

        return sessions

    def _get_session_rich_row(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single session with the same enriched columns as
        ``list_sessions_rich`` (preview + last_active). Returns None if the
        session doesn't exist.
        """
        query = """
            SELECT s.*,
                COALESCE(
                    (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                     FROM messages m
                     WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                     ORDER BY m.timestamp, m.id LIMIT 1),
                    ''
                ) AS _preview_raw,
                COALESCE(
                    (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                    s.started_at
                ) AS last_active
            FROM sessions s
            WHERE s.id = ?
        """
        with self._lock:
            cursor = self._conn.execute(query, (session_id,))
            row = cursor.fetchone()
        if not row:
            return None
        s = dict(row)
        raw = s.pop("_preview_raw", "").strip()
        if raw:
            text = raw[:60]
            s["preview"] = text + ("..." if len(raw) > 60 else "")
        else:
            s["preview"] = ""
        return s

    # =========================================================================
    # Хранение сообщений
    # =========================================================================

    # Сигнальный префикс, используемый для отличия структурированного контента
    # в кодировке JSON (мультимодальные сообщения: списки частей, такие как
    # текст + image_url) от простого текстового контента. Нулевой байт (\x00)
    # недопустим в обычном тексте, поэтому он не может конфликтовать с
    # реальным контентом пользователя.
    _CONTENT_JSON_PREFIX = "\x00json:"

    @classmethod
    def _encode_content(cls, content: Any) -> Any:
        """Сериализовать структурированный (список/словарь) контент сообщения для sqlite.

        sqlite3 может привязывать к параметрам запроса только типы ``str``,
        ``bytes``, ``int``, ``float`` и ``None``. Мультимодальные сообщения
        содержат ``content`` в виде списка частей
        (``[{"type": "text", ...}, {"type": "image_url", ...}]``), что вызывает
        ошибку ``ProgrammingError: Error binding parameter N: type 'list' is
        not supported`` при прямой привязке.

        Возвращает значение без изменений, если это уже безопасный скаляр, или
        строку JSON с сигнальным префиксом для списков/словарей. Используется
        в паре с :meth:`_decode_content` при чтении.
        """
        if content is None or isinstance(content, (str, bytes, int, float)):
            return content
        try:
            return cls._CONTENT_JSON_PREFIX + json.dumps(content)
        except (TypeError, ValueError):
            # Крайний случай: преобразуем в строку, чтобы сохранение не сорвалось.
            return str(content)

    @classmethod
    def _decode_content(cls, content: Any) -> Any:
        """Обратная операция для :meth:`_encode_content`; возвращает скаляры без изменений."""
        if isinstance(content, str) and content.startswith(cls._CONTENT_JSON_PREFIX):
            try:
                return json.loads(content[len(cls._CONTENT_JSON_PREFIX):])
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Не удалось декодировать контент сообщения в формате JSON; "
                    "возвращается необработанная строка"
                )
                return content
        return content

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str = None,
        tool_name: str = None,
        tool_calls: Any = None,
        tool_call_id: str = None,
        token_count: int = None,
        finish_reason: str = None,
        reasoning: str = None,
        reasoning_content: str = None,
        reasoning_details: Any = None,
        codex_reasoning_items: Any = None,
        codex_message_items: Any = None,
    ) -> int:
        """
        Добавить сообщение в сессию. Возвращает ID строки сообщения.

        Также увеличивает message_count сессии (и tool_call_count, если роль
        равна 'tool' или присутствует tool_calls).
        """
        # Сериализуем структурированные поля в JSON перед входом в транзакцию записи
        reasoning_details_json = (
            json.dumps(reasoning_details)
            if reasoning_details else None
        )
        codex_items_json = (
            json.dumps(codex_reasoning_items)
            if codex_reasoning_items else None
        )
        codex_message_items_json = (
            json.dumps(codex_message_items)
            if codex_message_items else None
        )
        tool_calls_json = json.dumps(tool_calls) if tool_calls else None
        # Мультимодальный контент (список частей) должен быть закодирован в JSON:
        # sqlite3 не может напрямую привязывать параметры-списки/словари.
        stored_content = self._encode_content(content)

        # Предварительно вычисляем количество вызовов инструментов
        num_tool_calls = 0
        if tool_calls is not None:
            num_tool_calls = len(tool_calls) if isinstance(tool_calls, list) else 1

        def _do(conn):
            cursor = conn.execute(
                """INSERT INTO messages (session_id, role, content, tool_call_id,
                   tool_calls, tool_name, timestamp, token_count, finish_reason,
                   reasoning, reasoning_content, reasoning_details, codex_reasoning_items,
                   codex_message_items)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    role,
                    stored_content,
                    tool_call_id,
                    tool_calls_json,
                    tool_name,
                    time.time(),
                    token_count,
                    finish_reason,
                    reasoning,
                    reasoning_content,
                    reasoning_details_json,
                    codex_items_json,
                    codex_message_items_json,
                ),
            )
            msg_id = cursor.lastrowid
            # Обновляем счетчики
            if num_tool_calls > 0:
                conn.execute(
                    """UPDATE sessions SET message_count = message_count + 1,
                       tool_call_count = tool_call_count + ? WHERE id = ?""",
                    (num_tool_calls, session_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                    (session_id,),
                )
            return msg_id

        return self._execute_write(_do)

    def replace_messages(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """Атомарно заменить все сообщения сессии.

        Используется в потоках перезаписи стенограммы, таких как /retry, /undo и /compress.
        Последовательность удаления и повторной вставки должна фиксироваться как одна
        транзакция, чтобы сбой в середине перезаписи не оставил в SQLite частичную стенограмму.
        """

        def _do(conn):
            conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            conn.execute(
                "UPDATE sessions SET message_count = 0, tool_call_count = 0 WHERE id = ?",
                (session_id,),
            )

            now_ts = time.time()
            total_messages = 0
            total_tool_calls = 0
            for msg in messages:
                role = msg.get("role", "unknown")
                tool_calls = msg.get("tool_calls")
                reasoning_details = msg.get("reasoning_details") if role == "assistant" else None
                codex_reasoning_items = (
                    msg.get("codex_reasoning_items") if role == "assistant" else None
                )
                codex_message_items = (
                    msg.get("codex_message_items") if role == "assistant" else None
                )

                reasoning_details_json = (
                    json.dumps(reasoning_details) if reasoning_details else None
                )
                codex_items_json = (
                    json.dumps(codex_reasoning_items) if codex_reasoning_items else None
                )
                codex_message_items_json = (
                    json.dumps(codex_message_items) if codex_message_items else None
                )
                tool_calls_json = json.dumps(tool_calls) if tool_calls else None

                conn.execute(
                    """INSERT INTO messages (session_id, role, content, tool_call_id,
                       tool_calls, tool_name, timestamp, token_count, finish_reason,
                       reasoning, reasoning_content, reasoning_details, codex_reasoning_items,
                       codex_message_items)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        role,
                        self._encode_content(msg.get("content")),
                        msg.get("tool_call_id"),
                        tool_calls_json,
                        msg.get("tool_name"),
                        now_ts,
                        msg.get("token_count"),
                        msg.get("finish_reason"),
                        msg.get("reasoning") if role == "assistant" else None,
                        msg.get("reasoning_content") if role == "assistant" else None,
                        reasoning_details_json,
                        codex_items_json,
                        codex_message_items_json,
                    ),
                )
                total_messages += 1
                if tool_calls is not None:
                    total_tool_calls += (
                        len(tool_calls) if isinstance(tool_calls, list) else 1
                    )
                now_ts += 1e-6

            conn.execute(
                "UPDATE sessions SET message_count = ?, tool_call_count = ? WHERE id = ?",
                (total_messages, total_tool_calls, session_id),
            )

        self._execute_write(_do)

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Загрузить все сообщения сессии, упорядоченные по временной метке."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp, id",
                (session_id,),
            )
            rows = cursor.fetchall()
        result = []
        for row in rows:
            msg = dict(row)
            if "content" in msg:
                msg["content"] = self._decode_content(msg["content"])
            if msg.get("tool_calls"):
                try:
                    msg["tool_calls"] = json.loads(msg["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Не удалось десериализовать tool_calls в get_messages, откат к []")
                    msg["tool_calls"] = []
            result.append(msg)
        return result

    def resolve_resume_session_id(self, session_id: str) -> str:
        """Перенаправить цель возобновления на сессию-потомка, содержащую сообщения.

        Сжатие контекста завершает текущую сессию и создает новую дочернюю
        (связанную через ``parent_session_id``). Курсор сброса сбрасывается,
        поэтому дочерняя сессия — это место, куда на самом деле попадают новые
        сообщения; родительская сессия оказывается с ``message_count = 0``,
        если сообщения не были сброшены в нее до сжатия. См. #15000.

        Этот помощник проходит вперед по ``parent_session_id`` от ``session_id`` и
        возвращает первого потомка в цепочке, у которого есть хотя бы одна строка
        сообщения. Если у исходной сессии уже есть сообщения или ни у одного потомка
        их нет, возвращается исходный ``session_id`` без изменений.

        Цепочка всегда проходится через ребенка, у которого ``started_at`` самый
        поздний; это соответствует форме одиночной цепочки, которую создает сжатие.
        Ограничение глубины (32) защищает от случайных циклов в некорректных данных.
        """
        if not session_id:
            return session_id

        with self._lock:
            # Если в этой сессии уже есть сообщения, перенаправлять нечего.
            try:
                row = self._conn.execute(
                    "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
            except Exception:
                return session_id
            if row is not None:
                return session_id

            # Обходим потомков: на каждом шаге выбираем самую недавно начатую
            # дочернюю сессию; останавливаемся, как только найдем сессию с сообщениями.
            current = session_id
            seen = {current}
            for _ in range(32):
                try:
                    child_row = self._conn.execute(
                        "SELECT id FROM sessions "
                        "WHERE parent_session_id = ? "
                        "ORDER BY started_at DESC, id DESC LIMIT 1",
                        (current,),
                    ).fetchone()
                except Exception:
                    return session_id
                if child_row is None:
                    return session_id
                child_id = child_row["id"] if hasattr(child_row, "keys") else child_row[0]
                if not child_id or child_id in seen:
                    return session_id
                seen.add(child_id)
                try:
                    msg_row = self._conn.execute(
                        "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1",
                        (child_id,),
                    ).fetchone()
                except Exception:
                    return session_id
                if msg_row is not None:
                    return child_id
                current = child_id
        return session_id


    def get_messages_as_conversation(
        self, session_id: str, include_ancestors: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Load messages in the OpenAI conversation format (role + content dicts).
        Used by the gateway to restore conversation history.
        """
        session_ids = [session_id]
        if include_ancestors:
            session_ids = self._session_lineage_root_to_tip(session_id)

        with self._lock:
            placeholders = ",".join("?" for _ in session_ids)
            rows = self._conn.execute(
                "SELECT role, content, tool_call_id, tool_calls, tool_name, "
                "finish_reason, reasoning, reasoning_content, reasoning_details, "
                "codex_reasoning_items, codex_message_items "
                f"FROM messages WHERE session_id IN ({placeholders}) ORDER BY timestamp, id",
                tuple(session_ids),
            ).fetchall()

        messages = []
        for row in rows:
            content = self._decode_content(row["content"])
            if row["role"] in {"user", "assistant"} and isinstance(content, str):
                content = sanitize_context(content).strip()
            msg = {"role": row["role"], "content": content}
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["tool_name"]:
                msg["tool_name"] = row["tool_name"]
            if row["tool_calls"]:
                try:
                    msg["tool_calls"] = json.loads(row["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to deserialize tool_calls in conversation replay, falling back to []")
                    msg["tool_calls"] = []
            # Restore reasoning fields on assistant messages so providers
            # that replay reasoning (OpenRouter, OpenAI, Nous) receive
            # coherent multi-turn reasoning context.
            if row["role"] == "assistant":
                if row["finish_reason"]:
                    msg["finish_reason"] = row["finish_reason"]
                if row["reasoning"]:
                    msg["reasoning"] = row["reasoning"]
                if row["reasoning_content"] is not None:
                    msg["reasoning_content"] = row["reasoning_content"]
                if row["reasoning_details"]:
                    try:
                        msg["reasoning_details"] = json.loads(row["reasoning_details"])
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("Failed to deserialize reasoning_details, falling back to None")
                        msg["reasoning_details"] = None
                if row["codex_reasoning_items"]:
                    try:
                        msg["codex_reasoning_items"] = json.loads(row["codex_reasoning_items"])
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("Failed to deserialize codex_reasoning_items, falling back to None")
                        msg["codex_reasoning_items"] = None
                if row["codex_message_items"]:
                    try:
                        msg["codex_message_items"] = json.loads(row["codex_message_items"])
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("Failed to deserialize codex_message_items, falling back to None")
                        msg["codex_message_items"] = None
            if include_ancestors and self._is_duplicate_replayed_user_message(messages, msg):
                continue
            messages.append(msg)
        return messages

    def _session_lineage_root_to_tip(self, session_id: str) -> List[str]:
        if not session_id:
            return [session_id]

        chain = []
        current = session_id
        seen = set()
        with self._lock:
            for _ in range(100):
                if not current or current in seen:
                    break
                seen.add(current)
                chain.append(current)
                row = self._conn.execute(
                    "SELECT parent_session_id FROM sessions WHERE id = ?",
                    (current,),
                ).fetchone()
                if row is None:
                    break
                current = row["parent_session_id"] if hasattr(row, "keys") else row[0]
        return list(reversed(chain)) or [session_id]

    @staticmethod
    def _is_duplicate_replayed_user_message(messages: List[Dict[str, Any]], msg: Dict[str, Any]) -> bool:
        if msg.get("role") != "user":
            return False
        content = msg.get("content")
        if not isinstance(content, str) or not content:
            return False
        for prev in reversed(messages):
            if prev.get("role") == "user" and prev.get("content") == content:
                return True
            if prev.get("role") == "assistant" and (prev.get("content") or prev.get("tool_calls")):
                return False
        return False

    # =========================================================================
    # Search
    # =========================================================================

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """Sanitize user input for safe use in FTS5 MATCH queries.

        FTS5 has its own query syntax where characters like ``"``, ``(``, ``)``,
        ``+``, ``*``, ``{``, ``}`` and bare boolean operators (``AND``, ``OR``,
        ``NOT``) have special meaning.  Passing raw user input directly to
        MATCH can cause ``sqlite3.OperationalError``.

        Strategy:
        - Preserve properly paired quoted phrases (``"exact phrase"``)
        - Strip unmatched FTS5-special characters that would cause errors
        - Wrap unquoted hyphenated and dotted terms in quotes so FTS5
          matches them as exact phrases instead of splitting on the
          hyphen/dot (e.g. ``chat-send``, ``P2.2``, ``my-app.config.ts``)
        """
        # Step 1: Extract balanced double-quoted phrases and protect them
        # from further processing via numbered placeholders.
        _quoted_parts: list = []

        def _preserve_quoted(m: re.Match) -> str:
            _quoted_parts.append(m.group(0))
            return f"\x00Q{len(_quoted_parts) - 1}\x00"

        sanitized = re.sub(r'"[^"]*"', _preserve_quoted, query)

        # Step 2: Strip remaining (unmatched) FTS5-special characters
        sanitized = re.sub(r'[+{}()\"^]', " ", sanitized)

        # Step 3: Collapse repeated * (e.g. "***") into a single one,
        # and remove leading * (prefix-only needs at least one char before *)
        sanitized = re.sub(r"\*+", "*", sanitized)
        sanitized = re.sub(r"(^|\s)\*", r"\1", sanitized)

        # Step 4: Remove dangling boolean operators at start/end that would
        # cause syntax errors (e.g. "hello AND" or "OR world")
        sanitized = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", sanitized.strip())
        sanitized = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", sanitized.strip())

        # Step 5: Wrap unquoted dotted and/or hyphenated terms in double
        # quotes.  FTS5's tokenizer splits on dots and hyphens, turning
        # ``chat-send`` into ``chat AND send`` and ``P2.2`` into ``p2 AND 2``.
        # Quoting preserves phrase semantics.  A single pass avoids the
        # double-quoting bug that would occur if dotted, hyphenated and underscored
        # patterns were applied sequentially (e.g. ``my-app.config``).
        sanitized = re.sub(r"\b(\w+(?:[._-]\w+)+)\b", r'"\1"', sanitized)

        # Step 6: Restore preserved quoted phrases
        for i, quoted in enumerate(_quoted_parts):
            sanitized = sanitized.replace(f"\x00Q{i}\x00", quoted)

        return sanitized.strip()


    @staticmethod
    def _is_cjk_codepoint(cp: int) -> bool:
        return (0x4E00 <= cp <= 0x9FFF or    # CJK Unified Ideographs
                0x3400 <= cp <= 0x4DBF or    # CJK Extension A
                0x20000 <= cp <= 0x2A6DF or  # CJK Extension B
                0x3000 <= cp <= 0x303F or    # CJK Symbols
                0x3040 <= cp <= 0x309F or    # Hiragana
                0x30A0 <= cp <= 0x30FF or    # Katakana
                0xAC00 <= cp <= 0xD7AF)      # Hangul Syllables

    @staticmethod
    def _contains_cjk(text: str) -> bool:
        """Check if text contains CJK (Chinese, Japanese, Korean) characters."""
        for ch in text:
            cp = ord(ch)
            if (0x4E00 <= cp <= 0x9FFF or    # CJK Unified Ideographs
                0x3400 <= cp <= 0x4DBF or    # CJK Extension A
                0x20000 <= cp <= 0x2A6DF or  # CJK Extension B
                0x3000 <= cp <= 0x303F or    # CJK Symbols
                0x3040 <= cp <= 0x309F or    # Hiragana
                0x30A0 <= cp <= 0x30FF or    # Katakana
                0xAC00 <= cp <= 0xD7AF):     # Hangul Syllables
                return True
        return False

    @classmethod
    def _count_cjk(cls, text: str) -> int:
        """Count CJK characters in text."""
        return sum(1 for ch in text if cls._is_cjk_codepoint(ord(ch)))

    def search_messages(
        self,
        query: str,
        source_filter: List[str] = None,
        exclude_sources: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Full-text search across session messages using FTS5.

        Supports FTS5 query syntax:
          - Simple keywords: "docker deployment"
          - Phrases: '"exact phrase"'
          - Boolean: "docker OR kubernetes", "python NOT java"
          - Prefix: "deploy*"

        Returns matching messages with session metadata, content snippet,
        and surrounding context (1 message before and after the match).
        """
        if not query or not query.strip():
            return []

        query = self._sanitize_fts5_query(query)
        if not query:
            return []

        # Build WHERE clauses dynamically
        where_clauses = ["messages_fts MATCH ?"]
        params: list = [query]

        if source_filter is not None:
            source_placeholders = ",".join("?" for _ in source_filter)
            where_clauses.append(f"s.source IN ({source_placeholders})")
            params.extend(source_filter)

        if exclude_sources is not None:
            exclude_placeholders = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({exclude_placeholders})")
            params.extend(exclude_sources)

        if role_filter:
            role_placeholders = ",".join("?" for _ in role_filter)
            where_clauses.append(f"m.role IN ({role_placeholders})")
            params.extend(role_filter)

        where_sql = " AND ".join(where_clauses)
        params.extend([limit, offset])

        sql = f"""
            SELECT
                m.id,
                m.session_id,
                m.role,
                snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                m.content,
                m.timestamp,
                m.tool_name,
                s.source,
                s.model,
                s.started_at AS session_started
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE {where_sql}
            ORDER BY rank
            LIMIT ? OFFSET ?
        """

        # CJK queries bypass the unicode61 FTS5 table.  The default tokenizer
        # splits CJK characters into individual tokens, so "大别山项目" becomes
        # "大 AND 别 AND 山 AND 项 AND 目" — producing false positives and
        # missing exact phrase matches.
        #
        # For queries with 3+ CJK characters, we use the trigram FTS5 table
        # (indexed substring matching with ranking and snippets).  For shorter
        # CJK queries (1-2 chars), trigram can't match (it needs ≥9 UTF-8
        # bytes = 3 CJK chars), so we fall back to LIKE.
        is_cjk = self._contains_cjk(query)
        if is_cjk:
            raw_query = query.strip('"').strip()
            cjk_count = self._count_cjk(raw_query)

            # Per-token CJK length check (#20494): trigram needs >=3 CJK chars
            # per token. A query like "广西 OR 桂林 OR 漓江" has cjk_count=6
            # (>=3) but each individual token is only 2 chars — trigram returns 0.
            # Route to LIKE when any non-operator CJK token is <3 CJK chars.
            _tokens_for_check = [
                t for t in raw_query.split()
                if t.upper() not in ("AND", "OR", "NOT") and self._contains_cjk(t)
            ]
            _any_short_cjk = any(
                self._count_cjk(t) < 3 for t in _tokens_for_check
            )

            if cjk_count >= 3 and not _any_short_cjk:
                # Trigram FTS5 path — quote each non-operator token to handle
                # FTS5 special chars (%, *, etc.) while preserving boolean
                # operators (AND, OR, NOT) for multi-term queries.
                tokens = raw_query.split()
                parts = []
                for tok in tokens:
                    if tok.upper() in ("AND", "OR", "NOT"):
                        parts.append(tok)
                    else:
                        parts.append('"' + tok.replace('"', '""') + '"')
                trigram_query = " ".join(parts)
                tri_where = ["messages_fts_trigram MATCH ?"]
                tri_params: list = [trigram_query]
                if source_filter is not None:
                    tri_where.append(f"s.source IN ({','.join('?' for _ in source_filter)})")
                    tri_params.extend(source_filter)
                if exclude_sources is not None:
                    tri_where.append(f"s.source NOT IN ({','.join('?' for _ in exclude_sources)})")
                    tri_params.extend(exclude_sources)
                if role_filter:
                    tri_where.append(f"m.role IN ({','.join('?' for _ in role_filter)})")
                    tri_params.extend(role_filter)
                tri_sql = f"""
                    SELECT
                        m.id,
                        m.session_id,
                        m.role,
                        snippet(messages_fts_trigram, 0, '>>>', '<<<', '...', 40) AS snippet,
                        m.content,
                        m.timestamp,
                        m.tool_name,
                        s.source,
                        s.model,
                        s.started_at AS session_started
                    FROM messages_fts_trigram
                    JOIN messages m ON m.id = messages_fts_trigram.rowid
                    JOIN sessions s ON s.id = m.session_id
                    WHERE {' AND '.join(tri_where)}
                    ORDER BY rank
                    LIMIT ? OFFSET ?
                """
                tri_params.extend([limit, offset])
                with self._lock:
                    try:
                        tri_cursor = self._conn.execute(tri_sql, tri_params)
                    except sqlite3.OperationalError:
                        matches = []
                    else:
                        matches = [dict(row) for row in tri_cursor.fetchall()]
            else:
                # Short / mixed CJK query: trigram cannot match tokens with
                # <3 CJK chars. Fall back to LIKE substring search.
                # For multi-token OR queries (e.g. "广西 OR 桂林 OR 漓江"),
                # build one LIKE condition per non-operator token so each term
                # is matched independently (#20494).
                non_op_tokens = [
                    t for t in raw_query.split()
                    if t.upper() not in ("AND", "OR", "NOT")
                ] or [raw_query]
                token_clauses = []
                like_params: list = []
                for tok in non_op_tokens:
                    esc = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    token_clauses.append(
                        "(m.content LIKE ? ESCAPE '\\' OR m.tool_name LIKE ? ESCAPE '\\' OR m.tool_calls LIKE ? ESCAPE '\\')"
                    )
                    like_params += [f"%{esc}%", f"%{esc}%", f"%{esc}%"]
                like_where = [f"({' OR '.join(token_clauses)})"]
                if source_filter is not None:
                    like_where.append(f"s.source IN ({','.join('?' for _ in source_filter)})")
                    like_params.extend(source_filter)
                if exclude_sources is not None:
                    like_where.append(f"s.source NOT IN ({','.join('?' for _ in exclude_sources)})")
                    like_params.extend(exclude_sources)
                if role_filter:
                    like_where.append(f"m.role IN ({','.join('?' for _ in role_filter)})")
                    like_params.extend(role_filter)
                like_sql = f"""
                    SELECT m.id, m.session_id, m.role,
                           substr(m.content,
                                  max(1, instr(m.content, ?) - 40),
                                  120) AS snippet,
                           m.content, m.timestamp, m.tool_name,
                           s.source, s.model, s.started_at AS session_started
                    FROM messages m
                    JOIN sessions s ON s.id = m.session_id
                    WHERE {' AND '.join(like_where)}
                    ORDER BY m.timestamp DESC
                    LIMIT ? OFFSET ?
                """
                like_params.extend([limit, offset])
                # instr() for snippet uses first search token
                like_params = [non_op_tokens[0]] + like_params
                with self._lock:
                    like_cursor = self._conn.execute(like_sql, like_params)
                    matches = [dict(row) for row in like_cursor.fetchall()]
        else:
            with self._lock:
                try:
                    cursor = self._conn.execute(sql, params)
                except sqlite3.OperationalError:
                    # FTS5 query syntax error despite sanitization — return empty
                    return []
                else:
                    matches = [dict(row) for row in cursor.fetchall()]

        # Add surrounding context (1 message before + after each match).
        # Done outside the lock so we don't hold it across N sequential queries.
        for match in matches:
            try:
                with self._lock:
                    ctx_cursor = self._conn.execute(
                        """WITH target AS (
                               SELECT session_id, timestamp, id
                               FROM messages
                               WHERE id = ?
                           )
                           SELECT role, content
                           FROM (
                               SELECT m.id, m.timestamp, m.role, m.content
                               FROM messages m
                               JOIN target t ON t.session_id = m.session_id
                               WHERE (m.timestamp < t.timestamp)
                                  OR (m.timestamp = t.timestamp AND m.id < t.id)
                               ORDER BY m.timestamp DESC, m.id DESC
                               LIMIT 1
                           )
                           UNION ALL
                           SELECT role, content
                           FROM messages
                           WHERE id = ?
                           UNION ALL
                           SELECT role, content
                           FROM (
                               SELECT m.id, m.timestamp, m.role, m.content
                               FROM messages m
                               JOIN target t ON t.session_id = m.session_id
                               WHERE (m.timestamp > t.timestamp)
                                  OR (m.timestamp = t.timestamp AND m.id > t.id)
                               ORDER BY m.timestamp ASC, m.id ASC
                               LIMIT 1
                           )""",
                        (match["id"], match["id"]),
                    )
                    context_msgs = []
                    for r in ctx_cursor.fetchall():
                        raw = r["content"]
                        decoded = self._decode_content(raw)
                        # Multimodal context: render a compact text-only
                        # summary for search previews.
                        if isinstance(decoded, list):
                            text_parts = [
                                p.get("text", "") for p in decoded
                                if isinstance(p, dict) and p.get("type") == "text"
                            ]
                            text = " ".join(t for t in text_parts if t).strip()
                            preview = text or "[multimodal content]"
                        elif isinstance(decoded, str):
                            preview = decoded
                        else:
                            preview = ""
                        context_msgs.append(
                            {"role": r["role"], "content": preview[:200]}
                        )
                match["context"] = context_msgs
            except Exception:
                match["context"] = []

        # Remove full content from result (snippet is enough, saves tokens)
        for match in matches:
            match.pop("content", None)

        return matches

    def search_sessions(
        self,
        source: str = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List sessions, optionally filtered by source.

        Returns rows enriched with a computed ``last_active`` column (latest
        message timestamp for the session, falling back to ``started_at``),
        ordered by most-recently-used first.
        """
        select_with_last_active = (
            "SELECT s.*, COALESCE(m.last_active, s.started_at) AS last_active "
            "FROM sessions s "
            "LEFT JOIN ("
            "SELECT session_id, MAX(timestamp) AS last_active "
            "FROM messages GROUP BY session_id"
            ") m ON m.session_id = s.id "
        )
        with self._lock:
            if source:
                cursor = self._conn.execute(
                    f"{select_with_last_active}"
                    "WHERE s.source = ? "
                    "ORDER BY last_active DESC, s.started_at DESC, s.id DESC LIMIT ? OFFSET ?",
                    (source, limit, offset),
                )
            else:
                cursor = self._conn.execute(
                    f"{select_with_last_active}"
                    "ORDER BY last_active DESC, s.started_at DESC, s.id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # Utility
    # =========================================================================

    def session_count(self, source: str = None) -> int:
        """Count sessions, optionally filtered by source."""
        with self._lock:
            if source:
                cursor = self._conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE source = ?", (source,)
                )
            else:
                cursor = self._conn.execute("SELECT COUNT(*) FROM sessions")
            return cursor.fetchone()[0]

    def message_count(self, session_id: str = None) -> int:
        """Count messages, optionally for a specific session."""
        with self._lock:
            if session_id:
                cursor = self._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
                )
            else:
                cursor = self._conn.execute("SELECT COUNT(*) FROM messages")
            return cursor.fetchone()[0]

    # =========================================================================
    # Export and cleanup
    # =========================================================================

    def export_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Export a single session with all its messages as a dict."""
        session = self.get_session(session_id)
        if not session:
            return None
        messages = self.get_messages(session_id)
        return {**session, "messages": messages}

    def export_all(self, source: str = None) -> List[Dict[str, Any]]:
        """
        Export all sessions (with messages) as a list of dicts.
        Suitable for writing to a JSONL file for backup/analysis.
        """
        sessions = self.search_sessions(source=source, limit=100000)
        results = []
        for session in sessions:
            messages = self.get_messages(session["id"])
            results.append({**session, "messages": messages})
        return results

    def clear_messages(self, session_id: str) -> None:
        """Delete all messages for a session and reset its counters."""
        def _do(conn):
            conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            conn.execute(
                "UPDATE sessions SET message_count = 0, tool_call_count = 0 WHERE id = ?",
                (session_id,),
            )
        self._execute_write(_do)

    @staticmethod
    def _remove_session_files(sessions_dir: Optional[Path], session_id: str) -> None:
        """Remove on-disk transcript files for a session.

        Cleans up ``{session_id}.json``, ``{session_id}.jsonl``, and any
        ``request_dump_{session_id}_*.json`` files left by the gateway.
        Silently skips files that don't exist and swallows OSError so a
        filesystem hiccup never blocks a DB operation.
        """
        if sessions_dir is None:
            return
        for suffix in (".json", ".jsonl"):
            p = sessions_dir / f"{session_id}{suffix}"
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        # request_dump files use session_id as a prefix component
        try:
            for p in sessions_dir.glob(f"request_dump_{session_id}_*.json"):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError:
            pass

    def delete_session(
        self,
        session_id: str,
        sessions_dir: Optional[Path] = None,
    ) -> bool:
        """Delete a session and all its messages.

        Child sessions are orphaned (parent_session_id set to NULL) rather
        than cascade-deleted, so they remain accessible independently.
        When *sessions_dir* is provided, also removes on-disk transcript
        files (``.json`` / ``.jsonl`` / ``request_dump_*``) for the deleted
        session. Returns True if the session was found and deleted.
        """
        def _do(conn):
            cursor = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ?", (session_id,)
            )
            if cursor.fetchone()[0] == 0:
                return False
            # Orphan child sessions so FK constraint is satisfied
            conn.execute(
                "UPDATE sessions SET parent_session_id = NULL "
                "WHERE parent_session_id = ?",
                (session_id,),
            )
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return True

        deleted = self._execute_write(_do)
        if deleted:
            self._remove_session_files(sessions_dir, session_id)
        return deleted

    def prune_sessions(
        self,
        older_than_days: int = 90,
        source: str = None,
        sessions_dir: Optional[Path] = None,
    ) -> int:
        """Delete sessions older than N days. Returns count of deleted sessions.

        Only prunes ended sessions (not active ones).  Child sessions outside
        the prune window are orphaned (parent_session_id set to NULL) rather
        than cascade-deleted.  When *sessions_dir* is provided, also removes
        on-disk transcript files (``.json`` / ``.jsonl`` /
        ``request_dump_*``) for every pruned session, outside the DB
        transaction.
        """
        cutoff = time.time() - (older_than_days * 86400)
        removed_ids: list[str] = []

        def _do(conn):
            if source:
                cursor = conn.execute(
                    """SELECT id FROM sessions
                       WHERE started_at < ? AND ended_at IS NOT NULL AND source = ?""",
                    (cutoff, source),
                )
            else:
                cursor = conn.execute(
                    "SELECT id FROM sessions WHERE started_at < ? AND ended_at IS NOT NULL",
                    (cutoff,),
                )
            session_ids = set(row["id"] for row in cursor.fetchall())

            if not session_ids:
                return 0

            # Orphan any sessions whose parent is about to be deleted
            placeholders = ",".join("?" * len(session_ids))
            conn.execute(
                f"UPDATE sessions SET parent_session_id = NULL "
                f"WHERE parent_session_id IN ({placeholders})",
                list(session_ids),
            )

            for sid in session_ids:
                conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
                conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
                removed_ids.append(sid)
            return len(session_ids)

        count = self._execute_write(_do)
        # Clean up on-disk files outside the DB transaction
        for sid in removed_ids:
            self._remove_session_files(sessions_dir, sid)
        return count

    # ── Meta key/value (for scheduler bookkeeping) ──

    def get_meta(self, key: str) -> Optional[str]:
        """Read a value from the state_meta key/value store."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM state_meta WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return row["value"] if isinstance(row, sqlite3.Row) else row[0]

    def set_meta(self, key: str, value: str) -> None:
        """Write a value to the state_meta key/value store."""
        def _do(conn):
            conn.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        self._execute_write(_do)

    def apply_telegram_topic_migration(self) -> None:
        """Create Telegram DM topic-mode tables on explicit /topic opt-in.

        This migration is deliberately not part of automatic SessionDB startup
        reconciliation. Operators must be able to upgrade Hermes, keep the old
        Telegram bot behavior running, and only mutate topic-mode state when the
        user executes /topic to opt into the feature.

        Schema versions:
          v1 — initial shape (no ON DELETE CASCADE on session_id FK)
          v2 — session_id FK gets ON DELETE CASCADE so session pruning
               automatically clears bindings.
        """
        def _do(conn):
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS telegram_dm_topic_mode (
                    chat_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    activated_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    has_topics_enabled INTEGER,
                    allows_users_to_create_topics INTEGER,
                    capability_checked_at REAL,
                    intro_message_id TEXT,
                    pinned_message_id TEXT
                );

                CREATE TABLE IF NOT EXISTS telegram_dm_topic_bindings (
                    chat_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    managed_mode TEXT NOT NULL DEFAULT 'auto',
                    linked_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, thread_id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_dm_topic_bindings_session
                ON telegram_dm_topic_bindings(session_id);

                CREATE INDEX IF NOT EXISTS idx_telegram_dm_topic_bindings_user
                ON telegram_dm_topic_bindings(user_id, chat_id);
                """
            )

            # v1 → v2: rebuild telegram_dm_topic_bindings if its session_id FK
            # lacks ON DELETE CASCADE. SQLite can't ALTER a foreign key, so we
            # rebuild the table. Only runs once per DB (version gate).
            current = conn.execute(
                "SELECT value FROM state_meta WHERE key = ?",
                ("telegram_dm_topic_schema_version",),
            ).fetchone()
            current_version = int(current[0]) if current and str(current[0]).isdigit() else 0
            if current_version < 2:
                fk_rows = conn.execute(
                    "PRAGMA foreign_key_list('telegram_dm_topic_bindings')"
                ).fetchall()
                needs_rebuild = any(
                    row[2] == "sessions" and (row[6] or "") != "CASCADE"
                    for row in fk_rows
                )
                if needs_rebuild:
                    conn.executescript(
                        """
                        CREATE TABLE telegram_dm_topic_bindings_new (
                            chat_id TEXT NOT NULL,
                            thread_id TEXT NOT NULL,
                            user_id TEXT NOT NULL,
                            session_key TEXT NOT NULL,
                            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                            managed_mode TEXT NOT NULL DEFAULT 'auto',
                            linked_at REAL NOT NULL,
                            updated_at REAL NOT NULL,
                            PRIMARY KEY (chat_id, thread_id)
                        );
                        INSERT INTO telegram_dm_topic_bindings_new
                            SELECT chat_id, thread_id, user_id, session_key,
                                   session_id, managed_mode, linked_at, updated_at
                            FROM telegram_dm_topic_bindings;
                        DROP TABLE telegram_dm_topic_bindings;
                        ALTER TABLE telegram_dm_topic_bindings_new
                            RENAME TO telegram_dm_topic_bindings;
                        CREATE UNIQUE INDEX idx_telegram_dm_topic_bindings_session
                            ON telegram_dm_topic_bindings(session_id);
                        CREATE INDEX idx_telegram_dm_topic_bindings_user
                            ON telegram_dm_topic_bindings(user_id, chat_id);
                        """
                    )

            conn.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("telegram_dm_topic_schema_version", "2"),
            )
        self._execute_write(_do)

    def enable_telegram_topic_mode(
        self,
        *,
        chat_id: str,
        user_id: str,
        has_topics_enabled: Optional[bool] = None,
        allows_users_to_create_topics: Optional[bool] = None,
    ) -> None:
        """Enable Telegram DM topic mode for one private chat/user.

        This method intentionally owns the explicit topic migration. Ordinary
        SessionDB startup must not create these side tables.
        """
        self.apply_telegram_topic_migration()
        now = time.time()

        def _to_int(value: Optional[bool]) -> Optional[int]:
            if value is None:
                return None
            return 1 if value else 0

        def _do(conn):
            conn.execute(
                """
                INSERT INTO telegram_dm_topic_mode (
                    chat_id, user_id, enabled, activated_at, updated_at,
                    has_topics_enabled, allows_users_to_create_topics,
                    capability_checked_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    enabled = 1,
                    updated_at = excluded.updated_at,
                    has_topics_enabled = excluded.has_topics_enabled,
                    allows_users_to_create_topics = excluded.allows_users_to_create_topics,
                    capability_checked_at = excluded.capability_checked_at
                """,
                (
                    str(chat_id),
                    str(user_id),
                    now,
                    now,
                    _to_int(has_topics_enabled),
                    _to_int(allows_users_to_create_topics),
                    now,
                ),
            )
        self._execute_write(_do)

    def disable_telegram_topic_mode(
        self,
        *,
        chat_id: str,
        clear_bindings: bool = True,
    ) -> None:
        """Disable Telegram DM topic mode for one private chat.

        When ``clear_bindings`` is True (default) the (chat_id, thread_id)
        bindings for this chat are also cleared so re-enabling later
        starts from a clean slate. Set to False if the operator wants to
        preserve bindings for a later re-enable.

        Never creates the topic-mode tables from scratch; if they don't
        exist there is nothing to disable and the call is a no-op.
        """
        def _do(conn):
            try:
                conn.execute(
                    "UPDATE telegram_dm_topic_mode SET enabled = 0, updated_at = ? "
                    "WHERE chat_id = ?",
                    (time.time(), str(chat_id)),
                )
                if clear_bindings:
                    conn.execute(
                        "DELETE FROM telegram_dm_topic_bindings WHERE chat_id = ?",
                        (str(chat_id),),
                    )
            except sqlite3.OperationalError:
                # Tables don't exist yet — nothing to disable.
                return
        self._execute_write(_do)

    def is_telegram_topic_mode_enabled(self, *, chat_id: str, user_id: str) -> bool:
        """Return whether Telegram DM topic mode is enabled for this chat/user."""
        with self._lock:
            try:
                row = self._conn.execute(
                    """
                    SELECT enabled FROM telegram_dm_topic_mode
                    WHERE chat_id = ? AND user_id = ?
                    """,
                    (str(chat_id), str(user_id)),
                ).fetchone()
            except sqlite3.OperationalError:
                return False
        if row is None:
            return False
        enabled = row["enabled"] if isinstance(row, sqlite3.Row) else row[0]
        return bool(enabled)

    def get_telegram_topic_binding(
        self,
        *,
        chat_id: str,
        thread_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the session binding for a Telegram DM topic, if present."""
        with self._lock:
            try:
                row = self._conn.execute(
                    """
                    SELECT * FROM telegram_dm_topic_bindings
                    WHERE chat_id = ? AND thread_id = ?
                    """,
                    (str(chat_id), str(thread_id)),
                ).fetchone()
            except sqlite3.OperationalError:
                return None
        return dict(row) if row else None

    def bind_telegram_topic(
        self,
        *,
        chat_id: str,
        thread_id: str,
        user_id: str,
        session_key: str,
        session_id: str,
        managed_mode: str = "auto",
    ) -> None:
        """Bind one Telegram DM topic thread to one Hermes session.

        A Hermes session may only be linked to one Telegram topic in MVP.
        Rebinding the same topic to the same session is idempotent; trying to
        link the same session to a different topic raises ValueError.
        """
        self.apply_telegram_topic_migration()
        now = time.time()
        chat_id = str(chat_id)
        thread_id = str(thread_id)
        user_id = str(user_id)
        session_key = str(session_key)
        session_id = str(session_id)

        def _do(conn):
            existing_session = conn.execute(
                """
                SELECT chat_id, thread_id FROM telegram_dm_topic_bindings
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if existing_session is not None:
                linked_chat = existing_session["chat_id"] if isinstance(existing_session, sqlite3.Row) else existing_session[0]
                linked_thread = existing_session["thread_id"] if isinstance(existing_session, sqlite3.Row) else existing_session[1]
                if str(linked_chat) != chat_id or str(linked_thread) != thread_id:
                    raise ValueError("session is already linked to another Telegram topic")

            conn.execute(
                """
                INSERT INTO telegram_dm_topic_bindings (
                    chat_id, thread_id, user_id, session_key, session_id,
                    managed_mode, linked_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    session_key = excluded.session_key,
                    session_id = excluded.session_id,
                    managed_mode = excluded.managed_mode,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_id,
                    thread_id,
                    user_id,
                    session_key,
                    session_id,
                    managed_mode,
                    now,
                    now,
                ),
            )
        self._execute_write(_do)

    def is_telegram_session_linked_to_topic(self, *, session_id: str) -> bool:
        """Return True if a Hermes session is already bound to any Telegram DM topic.

        Read-only: does NOT trigger the telegram-topic migration. If the
        topic-mode tables have not been created yet (i.e. nobody has run
        ``/topic`` in this profile), the session is by definition unbound
        and we return False.
        """
        with self._lock:
            try:
                row = self._conn.execute(
                    """
                    SELECT 1 FROM telegram_dm_topic_bindings
                    WHERE session_id = ?
                    LIMIT 1
                    """,
                    (str(session_id),),
                ).fetchone()
            except sqlite3.OperationalError:
                return False
        return row is not None

    def list_unlinked_telegram_sessions_for_user(
        self,
        *,
        chat_id: str,
        user_id: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """List previous Telegram sessions for this user that are not bound to a topic.

        Read-only: does NOT trigger the telegram-topic migration. If the
        topic-mode tables are absent, fall back to a simpler query that
        just returns this user's Telegram sessions — there can't be any
        bindings yet.
        """
        with self._lock:
            try:
                rows = self._conn.execute(
                    """
                    SELECT s.*,
                        COALESCE(
                            (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                             FROM messages m
                             WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                             ORDER BY m.timestamp, m.id LIMIT 1),
                            ''
                        ) AS _preview_raw,
                        COALESCE(
                            (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                            s.started_at
                        ) AS last_active
                    FROM sessions s
                    WHERE s.source = 'telegram'
                      AND s.user_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM telegram_dm_topic_bindings b
                          WHERE b.session_id = s.id
                      )
                    ORDER BY last_active DESC, s.started_at DESC
                    LIMIT ?
                    """,
                    (str(user_id), int(limit)),
                ).fetchall()
            except sqlite3.OperationalError:
                # telegram_dm_topic_bindings doesn't exist yet — no bindings
                # means every telegram session for this user is "unlinked".
                rows = self._conn.execute(
                    """
                    SELECT s.*,
                        COALESCE(
                            (SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63)
                             FROM messages m
                             WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
                             ORDER BY m.timestamp, m.id LIMIT 1),
                            ''
                        ) AS _preview_raw,
                        COALESCE(
                            (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
                            s.started_at
                        ) AS last_active
                    FROM sessions s
                    WHERE s.source = 'telegram'
                      AND s.user_id = ?
                    ORDER BY last_active DESC, s.started_at DESC
                    LIMIT ?
                    """,
                    (str(user_id), int(limit)),
                ).fetchall()

        sessions: List[Dict[str, Any]] = []
        for row in rows:
            session = dict(row)
            raw = str(session.pop("_preview_raw", "") or "").strip()
            session["preview"] = raw[:60] + ("..." if len(raw) > 60 else "") if raw else ""
            sessions.append(session)
        return sessions

    # ── Space reclamation ──

    def vacuum(self) -> None:
        """Run VACUUM to reclaim disk space after large deletes.

        SQLite does not shrink the database file when rows are deleted —
        freed pages just get reused on the next insert. After a prune that
        removed hundreds of sessions, the file stays bloated unless we
        explicitly VACUUM.

        VACUUM rewrites the entire DB, so it's expensive (seconds per
        100MB) and cannot run inside a transaction. It also acquires an
        exclusive lock, so callers must ensure no other writers are
        active. Safe to call at startup before the gateway/CLI starts
        serving traffic.
        """
        # VACUUM cannot be executed inside a transaction.
        with self._lock:
            # Best-effort WAL checkpoint first, then VACUUM.
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            self._conn.execute("VACUUM")

    def maybe_auto_prune_and_vacuum(
        self,
        retention_days: int = 90,
        min_interval_hours: int = 24,
        vacuum: bool = True,
        sessions_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Idempotent auto-maintenance: prune old sessions + optional VACUUM.

        Records the last run timestamp in state_meta so subsequent calls
        within ``min_interval_hours`` no-op. Designed to be called once at
        startup from long-lived entrypoints (CLI, gateway, cron scheduler).

        When *sessions_dir* is provided, on-disk transcript files
        (``.json`` / ``.jsonl`` / ``request_dump_*``) for pruned sessions
        are removed as part of the same sweep (issue #3015).

        Never raises. On any failure, logs a warning and returns a dict
        with ``"error"`` set.

        Returns a dict with keys:
          - ``"skipped"`` (bool) — true if within min_interval_hours of last run
          - ``"pruned"`` (int)   — number of sessions deleted
          - ``"vacuumed"`` (bool) — true if VACUUM ran
          - ``"error"`` (str, optional) — present only on failure
        """
        result: Dict[str, Any] = {"skipped": False, "pruned": 0, "vacuumed": False}
        try:
            # Skip if another process/call did maintenance recently.
            last_raw = self.get_meta("last_auto_prune")
            now = time.time()
            if last_raw:
                try:
                    last_ts = float(last_raw)
                    if now - last_ts < min_interval_hours * 3600:
                        result["skipped"] = True
                        return result
                except (TypeError, ValueError):
                    pass  # corrupt meta; treat as no prior run

            pruned = self.prune_sessions(
                older_than_days=retention_days,
                sessions_dir=sessions_dir,
            )
            result["pruned"] = pruned

            # Only VACUUM if we actually freed rows — VACUUM on a tight DB
            # is wasted I/O. Threshold keeps small DBs from paying the cost.
            if vacuum and pruned > 0:
                try:
                    self.vacuum()
                    result["vacuumed"] = True
                except Exception as exc:
                    logger.warning("state.db VACUUM failed: %s", exc)

            # Record the attempt even if pruned == 0, so we don't retry
            # every startup within the min_interval_hours window.
            self.set_meta("last_auto_prune", str(now))

            if pruned > 0:
                logger.info(
                    "state.db auto-maintenance: pruned %d session(s) older than %d days%s",
                    pruned,
                    retention_days,
                    " + VACUUM" if result["vacuumed"] else "",
                )
        except Exception as exc:
            # Maintenance must never block startup. Log and return error marker.
            logger.warning("state.db auto-maintenance failed: %s", exc)
            result["error"] = str(exc)

        return result

