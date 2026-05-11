"""Централизованная настройка логирования для Hermes Agent.

Предоставляет единую точку входа ``setup_logging()``, которую CLI и
шлюз вызывают в начале процесса запуска. Все файлы логов хранятся в
``~/.hermes/logs/`` (с учетом профилей через ``get_hermes_home()``).

Создаваемые файлы логов:
    agent.log   — INFO+, вся активность агента/инструментов/сессий (основной лог)
    errors.log  — WARNING+, только ошибки и предупреждения (быстрая диагностика)
    gateway.log — INFO+, события только шлюза (создается при mode="gateway")

Все файлы используют ``RotatingFileHandler`` с ``RedactingFormatter``, чтобы
секреты никогда не записывались на диск.

Разделение компонентов:
    gateway.log получает записи только от логгеров ``gateway.*`` —
    адаптеры платформ, управление сессиями, слэш-команды, доставка.
    agent.log остается общим логом (туда попадает всё).

Контекст сессии:
    Вызывайте ``set_session_context(session_id)`` в начале разговора
    и ``clear_session_context()`` по завершении. Все строки лога, созданные в
    этом потоке, будут включать ``[session_id]`` для фильтрации и корреляции.
"""

import logging
import os
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Sequence

from hermes_constants import get_config_path, get_hermes_home

# Флаг для отслеживания того, запускалась ли уже setup_logging(). Функция
# идемпотентна — повторный вызов безопасен, но ничего не делает,
# если не указано ``force=True``.
_logging_initialized = False

# Локальное хранилище потока для контекста сессии каждого разговора.
_session_context = threading.local()

# Формат лога по умолчанию — включает временную метку, уровень, необязательный тег сессии,
# имя логгера и сообщение. Поле ``%(session_tag)s`` гарантированно
# существует в каждой LogRecord благодаря _install_session_record_factory() ниже.
_LOG_FORMAT = "%(asctime)s %(levelname)s%(session_tag)s %(name)s: %(message)s"
_LOG_FORMAT_VERBOSE = "%(asctime)s - %(name)s - %(levelname)s%(session_tag)s - %(message)s"

# Сторонние логгеры, которые слишком многословны на уровнях DEBUG/INFO.
_NOISY_LOGGERS = (
    "openai",
    "openai._base_client",
    "httpx",
    "httpcore",
    "asyncio",
    "hpack",
    "hpack.hpack",
    "grpc",
    "modal",
    "urllib3",
    "urllib3.connectionpool",
    "websockets",
    "charset_normalizer",
    "markdown_it",
)


# ---------------------------------------------------------------------------
# Публичный API контекста сессии
# ---------------------------------------------------------------------------

def set_session_context(session_id: str) -> None:
    """Установить ID сессии для текущего потока.

    Все последующие записи лога в этом потоке будут включать ``[session_id]``
    в отформатированном выводе. Вызывайте в начале ``run_conversation()``.
    """
    _session_context.session_id = session_id


def clear_session_context() -> None:
    """Очистить ID сессии для текущего потока."""
    _session_context.session_id = None


# ---------------------------------------------------------------------------
# Фабрика записей — внедряет session_tag в каждую LogRecord при создании
# ---------------------------------------------------------------------------

def _install_session_record_factory() -> None:
    """Заменить глобальную фабрику LogRecord на ту, что добавляет ``session_tag``.

    В отличие от ``logging.Filter`` в обработчике или логгере, фабрика записей
    работает для КАЖДОЙ записи в процессе — включая записи от дочерних
    логгеров и записи, обрабатываемые сторонними обработчиками. Это
    гарантирует, что ``%(session_tag)s`` всегда доступен в строках формата.

    Идемпотентно — проверяет наличие атрибута-маркера, чтобы избежать
    повторного оборачивания при перезагрузке модуля.
    """
    current_factory = logging.getLogRecordFactory()
    if getattr(current_factory, "_hermes_session_injector", False):
        return  # уже установлена

    def _session_record_factory(*args, **kwargs):
        record = current_factory(*args, **kwargs)
        sid = getattr(_session_context, "session_id", None)
        record.session_tag = f" [{sid}]" if sid else ""  # type: ignore[attr-defined]
        return record

    _session_record_factory._hermes_session_injector = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(_session_record_factory)


# Установка немедленно при импорте — session_tag доступен во всех записях
# с этого момента, даже до вызова setup_logging().
_install_session_record_factory()


# ---------------------------------------------------------------------------
# Фильтры
# ---------------------------------------------------------------------------

class _ComponentFilter(logging.Filter):
    """Пропускать только записи, чье имя логгера начинается с одного из *префиксов*.

    Используется для маршрутизации записей шлюза в ``gateway.log``,
    оставляя ``agent.log`` как общий лог.
    """

    def __init__(self, prefixes: Sequence[str]) -> None:
        super().__init__()
        self._prefixes = tuple(prefixes)

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(self._prefixes)


# Префиксы имен логгеров, принадлежащих каждому компоненту.
# Используется _ComponentFilter и доступно для ``hermes logs --component``.
COMPONENT_PREFIXES = {
    "gateway": ("gateway",),
    "agent": ("agent", "run_agent", "model_tools", "batch_runner"),
    "tools": ("tools",),
    "cli": ("hermes_cli", "cli"),
    "cron": ("cron",),
}


# ---------------------------------------------------------------------------
# Основная настройка
# ---------------------------------------------------------------------------

def setup_logging(
    *,
    hermes_home: Optional[Path] = None,
    log_level: Optional[str] = None,
    max_size_mb: Optional[int] = None,
    backup_count: Optional[int] = None,
    mode: Optional[str] = None,
    force: bool = False,
) -> Path:
    """Настроить подсистему логирования Hermes.

    Безопасно вызывать несколько раз — второй вызов ничего не делает,
    если *force* не установлен в ``True``.

    Параметры
    ----------
    hermes_home
        Переопределение домашней директории Hermes. По умолчанию используется
        ``get_hermes_home()`` (с учетом профилей).
    log_level
        Минимальный уровень для обработчика файла ``agent.log``. Принимает любое
        стандартное имя уровня Python (``"DEBUG"``, ``"INFO"``, ``"WARNING"``).
        По умолчанию ``"INFO"`` или значение из config.yaml ``logging.level``.
    max_size_mb
        Максимальный размер каждого файла лога в мегабайтах перед ротацией.
        По умолчанию 5 или значение из config.yaml ``logging.max_size_mb``.
    backup_count
        Количество хранимых файлов резервных копий логов.
        По умолчанию 3 или значение из config.yaml ``logging.backup_count``.
    mode
        Контекст вызывающего объекта: ``"cli"``, ``"gateway"``, ``"cron"``.
        При ``"gateway"`` создается дополнительный файл ``gateway.log``,
        в который попадают только записи компонента шлюза.
    force
        Запустить настройку заново, даже если она уже была вызвана.

    Возвращает
    -------
    Path
        Директория ``logs/``, в которую записываются файлы.
    """
    global _logging_initialized
    home = hermes_home or get_hermes_home()
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Чтение настроек по умолчанию из конфига (по возможности — конфиг может быть еще не загружен).
    cfg_level, cfg_max_size, cfg_backup = _read_logging_config()

    level_name = (log_level or cfg_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    max_bytes = (max_size_mb or cfg_max_size or 5) * 1024 * 1024
    backups = backup_count or cfg_backup or 3

    # Ленивый импорт во избежание циклической зависимости при загрузке модуля.
    from agent.redact import RedactingFormatter

    root = logging.getLogger()

    # --- agent.log (INFO+) — основной лог активности -------------------------
    _add_rotating_handler(
        root,
        log_dir / "agent.log",
        level=level,
        max_bytes=max_bytes,
        backup_count=backups,
        formatter=RedactingFormatter(_LOG_FORMAT),
    )

    # --- errors.log (WARNING+) — лог для быстрой диагностики ошибок ----------
    _add_rotating_handler(
        root,
        log_dir / "errors.log",
        level=logging.WARNING,
        max_bytes=2 * 1024 * 1024,
        backup_count=2,
        formatter=RedactingFormatter(_LOG_FORMAT),
    )

    # --- gateway.log (INFO+, только компонент шлюза) ------------------------
    if mode == "gateway":
        _add_rotating_handler(
            root,
            log_dir / "gateway.log",
            level=logging.INFO,
            max_bytes=5 * 1024 * 1024,
            backup_count=3,
            formatter=RedactingFormatter(_LOG_FORMAT),
            log_filter=_ComponentFilter(COMPONENT_PREFIXES["gateway"]),
        )

    if _logging_initialized and not force:
        return log_dir

    # Убедиться, что уровень корневого логгера достаточно низок для срабатывания обработчиков.
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)

    # Подавить многословные сторонние логгеры.
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _logging_initialized = True
    return log_dir


def setup_verbose_logging() -> None:
    """Включить логирование в консоль на уровне DEBUG для режима ``--verbose`` / ``-v``.

    Вызывается из ``AIAgent.__init__()`` при ``verbose_logging=True``.
    """
    from agent.redact import RedactingFormatter

    root = logging.getLogger()

    # Избежать добавления дублирующих потоковых обработчиков.
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler):
            if getattr(h, "_hermes_verbose", False):
                return

    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(RedactingFormatter(_LOG_FORMAT_VERBOSE, datefmt="%H:%M:%S"))
    handler._hermes_verbose = True  # type: ignore[attr-defined]
    root.addHandler(handler)

    # Снизить уровень корневого логгера, чтобы записи DEBUG доходили до всех обработчиков.
    if root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)

    # Оставить сторонние библиотеки на уровне WARNING для уменьшения шума.
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # rex-deploy на уровне INFO для статуса песочницы.
    logging.getLogger("rex-deploy").setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Внутренние помощники
# ---------------------------------------------------------------------------

class _ManagedRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler, обеспечивающий права записи для группы в управляемом режиме.

    В управляемом режиме (NixOS) stateDir использует setgid (2770), поэтому новые файлы
    наследуют группу hermes. Однако _open() и doRollover() создают файлы через open(),
    который использует umask процесса — обычно 0022, что дает 0644. Этот подкласс
    применяет chmod 0660 после обеих операций, чтобы шлюз и интерактивные пользователи
    могли совместно использовать файлы логов.
    """

    def __init__(self, *args, **kwargs):
        from hermes_cli.config import is_managed
        self._managed = is_managed()
        super().__init__(*args, **kwargs)

    def _chmod_if_managed(self):
        if self._managed:
            try:
                os.chmod(self.baseFilename, 0o660)
            except OSError:
                pass

    def _open(self):
        stream = super()._open()
        self._chmod_if_managed()
        return stream

    def doRollover(self):
        super().doRollover()
        self._chmod_if_managed()


def _add_rotating_handler(
    logger: logging.Logger,
    path: Path,
    *,
    level: int,
    max_bytes: int,
    backup_count: int,
    formatter: logging.Formatter,
    log_filter: Optional[logging.Filter] = None,
) -> None:
    """Добавить ``RotatingFileHandler`` к *logger*, пропуская, если он уже
    существует для того же пути к файлу (идемпотентно).

    Параметры
    ----------
    log_filter
        Необязательный фильтр для добавления к обработчику (например, ``_ComponentFilter``
        для gateway.log).
    """
    resolved = path.resolve()
    for existing in logger.handlers:
        if (
            isinstance(existing, RotatingFileHandler)
            and Path(getattr(existing, "baseFilename", "")).resolve() == resolved
        ):
            return  # уже добавлен

    path.parent.mkdir(parents=True, exist_ok=True)
    handler = _ManagedRotatingFileHandler(
        str(path), maxBytes=max_bytes, backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    if log_filter is not None:
        handler.addFilter(log_filter)
    logger.addHandler(handler)


def _read_logging_config():
    """Чтение ``logging.*`` из config.yaml по возможности.

    Возвращает ``(level, max_size_mb, backup_count)`` — любое значение может быть ``None``.
    """
    try:
        import yaml
        config_path = get_config_path()
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            log_cfg = cfg.get("logging", {})
            if isinstance(log_cfg, dict):
                return (
                    log_cfg.get("level"),
                    log_cfg.get("max_size_mb"),
                    log_cfg.get("backup_count"),
                )
    except Exception:
        pass
    return (None, None, None)
