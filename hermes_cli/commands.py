"""Определения слэш-команд и автодополнение для Hermes CLI.

Центральный реестр всех слэш-команд. Все потребители — справка CLI, диспетчеризация шлюза,
Telegram BotCommands, маппинг подкоманд Slack, автодополнение — получают данные из ``COMMAND_REGISTRY``.

Чтобы добавить команду: добавьте запись ``CommandDef`` в ``COMMAND_REGISTRY``.
Чтобы добавить алиас: установите ``aliases=("short",)`` для существующей ``CommandDef``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from utils import is_truthy_value

logger = logging.getLogger(__name__)

# prompt_toolkit является необязательной зависимостью CLI — нужен только для
# SlashCommandCompleter и SlashCommandAutoSuggest. Шлюз и тестовые
# среды, в которых он отсутствует, должны иметь возможность импортировать этот модуль
# для resolve_command, gateway_help_lines и COMMAND_REGISTRY.
try:
    from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
    from prompt_toolkit.completion import Completer, Completion
except ImportError:  # pragma: no cover
    AutoSuggest = object  # type: ignore[assignment,misc]
    Completer = object    # type: ignore[assignment,misc]
    Suggestion = None     # type: ignore[assignment]
    Completion = None     # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Dataclass CommandDef
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandDef:
    """Определение одной слэш-команды."""

    name: str                          # каноническое имя без слэша: "background"
    description: str                   # человекочитаемое описание
    category: str                      # "Сеанс", "Конфигурация" и т.д.
    aliases: tuple[str, ...] = ()      # альтернативные имена: ("bg",)
    args_hint: str = ""                # подсказка для аргументов: "<prompt>", "[name]"
    subcommands: tuple[str, ...] = ()  # подкоманды для автодополнения по Tab
    cli_only: bool = False             # доступно только в CLI
    gateway_only: bool = False         # доступно только в шлюзе/сообщениях
    gateway_config_gate: str | None = None  # путь в конфиге; если истинно, переопределяет cli_only для шлюза


# ---------------------------------------------------------------------------
# Центральный реестр — единый источник истины
# ---------------------------------------------------------------------------

COMMAND_REGISTRY: list[CommandDef] = [
    # Сеанс
    CommandDef("new", "Начать новый сеанс (новый ID сеанса + история)", "Сеанс",
               aliases=("reset",), args_hint="[название]"),
    CommandDef("topic", "Включить или проверить сеансы в топиках Telegram", "Сеанс",
               gateway_only=True, args_hint="[off|help|session-id]"),
    CommandDef("clear", "Очистить экран и начать новый сеанс", "Сеанс",
               cli_only=True),
    CommandDef("redraw", "Принудительно перерисовать интерфейс (исправляет смещение терминала)", "Сеанс",
               cli_only=True),
    CommandDef("history", "Показать историю разговора", "Сеанс",
               cli_only=True),
    CommandDef("save", "Сохранить текущий разговор", "Сеанс",
               cli_only=True),
    CommandDef("retry", "Повторить последнее сообщение (отправить агенту снова)", "Сеанс"),
    CommandDef("undo", "Удалить последний обмен сообщениями (пользователь/ассистент)", "Сеанс"),
    CommandDef("title", "Установить название для текущего сеанса", "Сеанс",
               args_hint="[название]"),
    CommandDef("branch", "Ответвить текущий сеанс (исследовать другой путь)", "Сеанс",
               aliases=("fork",), args_hint="[название]"),
    CommandDef("compress", "Вручную сжать контекст разговора", "Сеанс",
               args_hint="[тема фокуса]"),
    CommandDef("rollback", "Список или восстановление контрольных точек файловой системы", "Сеанс",
               args_hint="[номер]"),
    CommandDef("snapshot", "Создать или восстановить снимки состояния Hermes", "Сеанс",
               cli_only=True, aliases=("snap",), args_hint="[create|restore <id>|prune]"),
    CommandDef("stop", "Остановить все запущенные фоновые процессы", "Сеанс"),
    CommandDef("approve", "Одобрить ожидающую опасную команду", "Сеанс",
               gateway_only=True, args_hint="[session|always]"),
    CommandDef("deny", "Отклонить ожидающую опасную команду", "Сеанс",
               gateway_only=True),
    CommandDef("background", "Запустить запрос в фоновом режиме", "Сеанс",
               aliases=("bg", "btw"), args_hint="<запрос>"),
    CommandDef("agents", "Показать активных агентов и выполняемые задачи", "Сеанс",
               aliases=("tasks",)),
    CommandDef("queue", "Добавить запрос в очередь для следующего хода (без прерывания)", "Сеанс",
               aliases=("q",), args_hint="<запрос>"),
    CommandDef("steer", "Вставить сообщение после следующего вызова инструмента без прерывания", "Сеанс",
               args_hint="<запрос>"),
    CommandDef("goal", "Установить постоянную цель, над которой Hermes будет работать до достижения", "Сеанс",
               args_hint="[текст | pause | resume | clear | status]"),
    CommandDef("status", "Показать информацию о сеансе", "Сеанс"),
    CommandDef("whoami", "Показать уровень доступа к слэш-командам (админ / пользователь)", "Информация"),
    CommandDef("profile", "Показать имя активного профиля и домашний каталог", "Информация"),
    CommandDef("sethome", "Установить этот чат как главный канал", "Сеанс",
               gateway_only=True, aliases=("set-home",)),
    CommandDef("resume", "Возобновить ранее именованный сеанс", "Сеанс",
               args_hint="[название]"),

    # Конфигурация
    CommandDef("sessions", "Просмотр и возобновление предыдущих сеансов", "Сеанс"),

    # Конфигурация
    CommandDef("config", "Показать текущую конфигурацию", "Конфигурация",
               cli_only=True),
    CommandDef("model", "Сменить модель для этого сеанса", "Конфигурация",
               aliases=("provider",), args_hint="[модель] [--provider имя] [--global]"),
    CommandDef("gquota", "Показать использование квоты Google Gemini Code Assist", "Информация",
               cli_only=True),

    CommandDef("personality", "Установить предустановленную личность", "Конфигурация",
               args_hint="[имя]"),
    CommandDef("statusbar", "Переключить панель состояния контекста/модели", "Конфигурация",
               cli_only=True, aliases=("sb",)),
    CommandDef("verbose", "Переключить отображение прогресса: off -> new -> all -> verbose",
               "Конфигурация", cli_only=True,
               gateway_config_gate="display.tool_progress_command"),
    CommandDef("footer", "Переключить футер метаданных шлюза в финальных ответах",
               "Конфигурация", args_hint="[on|off|status]",
               subcommands=("on", "off", "status")),
    CommandDef("yolo", "Переключить режим YOLO (пропуск одобрения всех опасных команд)",
               "Конфигурация"),
    CommandDef("reasoning", "Управление усилиями и отображением рассуждений", "Конфигурация",
               args_hint="[уровень|show|hide]",
               subcommands=("none", "minimal", "low", "medium", "high", "xhigh", "show", "hide", "on", "off")),
    CommandDef("fast", "Переключить быстрый режим — OpenAI Priority / Anthropic Fast", "Конфигурация",
               args_hint="[normal|fast|status]",
               subcommands=("normal", "fast", "status", "on", "off")),
    CommandDef("skin", "Показать или изменить скин/тему интерфейса", "Конфигурация",
               cli_only=True, args_hint="[имя]"),
    CommandDef("indicator", "Выбрать стиль индикатора занятости TUI", "Конфигурация",
               cli_only=True, args_hint="[kaomoji|emoji|unicode|ascii]",
               subcommands=("kaomoji", "emoji", "unicode", "ascii")),
    CommandDef("voice", "Переключить голосовой режим", "Конфигурация",
               args_hint="[on|off|tts|status]", subcommands=("on", "off", "tts", "status")),
    CommandDef("busy", "Управление действием Enter во время работы Hermes", "Конфигурация",
               cli_only=True, args_hint="[queue|steer|interrupt|status]",
               subcommands=("queue", "steer", "interrupt", "status")),

    # Инструменты и Навыки
    CommandDef("tools", "Управление инструментами: /tools [list|disable|enable] [имя...]", "Инструменты и Навыки",
               args_hint="[list|disable|enable] [имя...]", cli_only=True),
    CommandDef("toolsets", "Список доступных наборов инструментов", "Инструменты и Навыки",
               cli_only=True),
    CommandDef("skills", "Поиск, установка, осмотр или управление навыками",
               "Инструменты и Навыки", cli_only=True,
               subcommands=("search", "browse", "inspect", "install")),
    CommandDef("cron", "Управление запланированными задачами", "Инструменты и Навыки",
               cli_only=True, args_hint="[подкоманда]",
               subcommands=("list", "add", "create", "edit", "pause", "resume", "run", "remove")),
    CommandDef("curator", "Фоновое обслуживание навыков (status, run, pin, archive)",
               "Инструменты и Навыки", args_hint="[подкоманда]",
               subcommands=("status", "run", "pause", "resume", "pin", "unpin", "restore", "list-archived")),
    CommandDef("kanban", "Доска для совместной работы (задачи, ссылки, комментарии)",
               "Инструменты и Навыки", args_hint="[подкоманда]",
               subcommands=("list", "ls", "show", "create", "assign", "link", "unlink",
                            "claim", "comment", "complete", "block", "unblock", "archive",
                            "tail", "dispatch", "context", "init", "gc")),
    CommandDef("reload", "Перезагрузить переменные .env в текущий сеанс", "Инструменты и Навыки",
               cli_only=True),
    CommandDef("reload-mcp", "Перезагрузить серверы MCP из конфигурации", "Инструменты и Навыки",
               aliases=("reload_mcp",)),
    CommandDef("reload-skills", "Пересканировать ~/.hermes/skills/ на наличие новых навыков",
               "Инструменты и Навыки", aliases=("reload_skills",)),
    CommandDef("browser", "Подключить инструменты браузера к Chrome через CDP", "Инструменты и Навыки",
               cli_only=True, args_hint="[connect|disconnect|status]",
               subcommands=("connect", "disconnect", "status")),
    CommandDef("plugins", "Список установленных плагинов и их статус",
               "Инструменты и Навыки", cli_only=True),

    # Информация
    CommandDef("commands", "Просмотр всех команд и навыков (с пагинацией)", "Информация",
               gateway_only=True, args_hint="[страница]"),
    CommandDef("help", "Показать доступные команды", "Информация"),
    CommandDef("restart", "Мягкий перезапуск шлюза после завершения активных задач", "Сеанс",
               gateway_only=True),
    CommandDef("usage", "Показать использование токенов и лимиты скорости сеанса", "Информация"),
    CommandDef("insights", "Показать аналитику использования", "Информация",
               args_hint="[дни]"),
    CommandDef("platforms", "Показать статус платформ шлюза/сообщений", "Информация",
               cli_only=True, aliases=("gateway",)),
    CommandDef("copy", "Копировать последний ответ ассистента в буфер обмена", "Информация",
               cli_only=True, args_hint="[номер]"),
    CommandDef("paste", "Прикрепить изображение из буфера обмена", "Информация",
               cli_only=True),
    CommandDef("image", "Прикрепить локальный файл изображения для следующего запроса", "Информация",
               cli_only=True, args_hint="<путь>"),
    CommandDef("update", "Обновить Hermes Agent до последней версии", "Информация",
               gateway_only=True),
    CommandDef("debug", "Загрузить отчет об отладке (системная инфо + логи) и получить ссылки", "Информация"),

    # Выход
    CommandDef("quit", "Выйти из CLI", "Выход",
               cli_only=True, aliases=("exit",)),
]


# ---------------------------------------------------------------------------
# Производные лукапы — перестраиваются при импорте
# ---------------------------------------------------------------------------

def _build_command_lookup() -> dict[str, CommandDef]:
    """Маппинг имен и алиасов в CommandDef."""
    lookup: dict[str, CommandDef] = {}
    for cmd in COMMAND_REGISTRY:
        lookup[cmd.name] = cmd
        for alias in cmd.aliases:
            lookup[alias] = cmd
    return lookup


_COMMAND_LOOKUP: dict[str, CommandDef] = _build_command_lookup()


def resolve_command(name: str) -> CommandDef | None:
    """Разрешить имя команды или алиас в CommandDef.

    Принимает имена со слэшем или без.
    """
    return _COMMAND_LOOKUP.get(name.lower().lstrip("/"))


def _build_description(cmd: CommandDef) -> str:
    """Построить строку описания для CLI, включая подсказку по использованию."""
    if cmd.args_hint:
        return f"{cmd.description} (использование: /{cmd.name} {cmd.args_hint})"
    return cmd.description


# Обратная совместимость: плоский словарь "/command" -> description
COMMANDS: dict[str, str] = {}
for _cmd in COMMAND_REGISTRY:
    if not _cmd.gateway_only:
        COMMANDS[f"/{_cmd.name}"] = _build_description(_cmd)
        for _alias in _cmd.aliases:
            COMMANDS[f"/{_alias}"] = f"{_cmd.description} (алиас для /{_cmd.name})"

# Обратная совместимость: категоризированный словарь
COMMANDS_BY_CATEGORY: dict[str, dict[str, str]] = {}
for _cmd in COMMAND_REGISTRY:
    if not _cmd.gateway_only:
        _cat = COMMANDS_BY_CATEGORY.setdefault(_cmd.category, {})
        _cat[f"/{_cmd.name}"] = COMMANDS[f"/{_cmd.name}"]
        for _alias in _cmd.aliases:
            _cat[f"/{_alias}"] = COMMANDS[f"/{_alias}"]


# Лукап подкоманд: "/cmd" -> ["sub1", "sub2", ...]
SUBCOMMANDS: dict[str, list[str]] = {}
for _cmd in COMMAND_REGISTRY:
    if _cmd.subcommands:
        SUBCOMMANDS[f"/{_cmd.name}"] = list(_cmd.subcommands)

# Также извлекаем подкоманды из args_hint через паттерны с вертикальной чертой
# напр. args_hint="[on|off|tts|status]" для команд без явных подкоманд.
_PIPE_SUBS_RE = re.compile(r"[a-z]+(?:\|[a-z]+)+")
for _cmd in COMMAND_REGISTRY:
    key = f"/{_cmd.name}"
    if key in SUBCOMMANDS or not _cmd.args_hint:
        continue
    m = _PIPE_SUBS_RE.search(_cmd.args_hint)
    if m:
        SUBCOMMANDS[key] = m.group(0).split("|")


# ---------------------------------------------------------------------------
# Помощники для шлюза
# ---------------------------------------------------------------------------

# Множество всех имен команд + алиасов, распознаваемых шлюзом.
GATEWAY_KNOWN_COMMANDS: frozenset[str] = frozenset(
    name
    for cmd in COMMAND_REGISTRY
    if not cmd.cli_only or cmd.gateway_config_gate
    for name in (cmd.name, *cmd.aliases)
)


def is_gateway_known_command(name: str | None) -> bool:
    """Возвращает True, если ``name`` является слэш-командой шлюза."""
    if not name:
        return False
    if name in GATEWAY_KNOWN_COMMANDS:
        return True
    for plugin_name, _description, _args_hint in _iter_plugin_command_entries():
        if plugin_name == name:
            return True
    return False


# Команды с явными обработчиками в gateway/run.py.
ACTIVE_SESSION_BYPASS_COMMANDS: frozenset[str] = frozenset(
    {
        "agents",
        "approve",
        "background",
        "commands",
        "deny",
        "help",
        "new",
        "profile",
        "queue",
        "restart",
        "status",
        "steer",
        "stop",
        "update",
    }
)


def should_bypass_active_session(command_name: str | None) -> bool:
    """Возвращает True для любой разрешимой слэш-команды."""
    return resolve_command(command_name) is not None if command_name else False


def _resolve_config_gates() -> set[str]:
    """Возвращает канонические имена команд, чьи ``gateway_config_gate`` истинны."""
    gated = [c for c in COMMAND_REGISTRY if c.gateway_config_gate]
    if not gated:
        return set()
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
    except Exception:
        return set()
    result: set[str] = set()
    for cmd in gated:
        val: Any = cfg
        for key in cmd.gateway_config_gate.split("."):
            if isinstance(val, dict):
                val = val.get(key)
            else:
                val = None
                break
        if is_truthy_value(val, default=False):
            result.add(cmd.name)
    return result


def _is_gateway_available(cmd: CommandDef, config_overrides: set[str] | None = None) -> bool:
    """Проверяет, должна ли команда *cmd* появляться в интерфейсах шлюза."""
    if not cmd.cli_only:
        return True
    if cmd.gateway_config_gate:
        overrides = config_overrides if config_overrides is not None else _resolve_config_gates()
        return cmd.name in overrides
    return False


def _requires_argument(args_hint: str) -> bool:
    """Возвращает True, если выбор команды без текста был бы неполным."""
    return args_hint.strip().startswith("<")


def gateway_help_lines() -> list[str]:
    """Генерирует строки справки шлюза из реестра."""
    overrides = _resolve_config_gates()
    lines: list[str] = []
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        args = f" {cmd.args_hint}" if cmd.args_hint else ""
        alias_parts: list[str] = []
        for a in cmd.aliases:
            if a.replace("-", "_") == cmd.name.replace("-", "_") and a != cmd.name:
                continue
            alias_parts.append(f"`/{a}`")
        alias_note = f" (алиас: {', '.join(alias_parts)})" if alias_parts else ""
        lines.append(f"`/{cmd.name}{args}` -- {cmd.description}{alias_note}")
    return lines


def _iter_plugin_command_entries() -> list[tuple[str, str, str]]:
    """Возвращает (name, description, args_hint) для всех команд плагинов."""
    try:
        from hermes_cli.plugins import get_plugin_commands
    except Exception:
        return []
    try:
        commands = get_plugin_commands() or {}
    except Exception:
        return []
    entries: list[tuple[str, str, str]] = []
    for name, meta in commands.items():
        if not isinstance(name, str) or not isinstance(meta, dict):
            continue
        description = str(meta.get("description") or f"Запустить /{name}")
        args_hint = str(meta.get("args_hint") or "").strip()
        entries.append((name, description, args_hint))
    return entries


def telegram_bot_commands() -> list[tuple[str, str]]:
    """Возвращает пары (command_name, description) для Telegram setMyCommands."""
    overrides = _resolve_config_gates()
    result: list[tuple[str, str]] = []
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        if _requires_argument(cmd.args_hint):
            continue
        tg_name = _sanitize_telegram_name(cmd.name)
        if tg_name:
            result.append((tg_name, cmd.description))
    for name, description, args_hint in _iter_plugin_command_entries():
        if _requires_argument(args_hint):
            continue
        tg_name = _sanitize_telegram_name(name)
        if tg_name:
            result.append((tg_name, description))
    return result


_CMD_NAME_LIMIT = 32
"""Максимальная длина имени команды для Telegram и Discord."""

_TG_NAME_LIMIT = _CMD_NAME_LIMIT

_TG_INVALID_CHARS = re.compile(r"[^a-z0-9_]")
_TG_MULTI_UNDERSCORE = re.compile(r"_{2,}")


def _sanitize_telegram_name(raw: str) -> str:
    """Преобразовать имя команды/навыка/плагина в валидное имя команды Telegram."""
    name = raw.lower().replace("-", "_")
    name = _TG_INVALID_CHARS.sub("", name)
    name = _TG_MULTI_UNDERSCORE.sub("_", name)
    return name.strip("_")


def _clamp_command_names(
    entries: list[tuple[str, ...]],
    reserved: set[str],
) -> list[tuple[str, ...]]:
    """Обеспечить лимит 32 символа для имени команды с избеганием коллизий."""
    used: set[str] = set(reserved)
    result: list[tuple] = []
    for entry in entries:
        name, desc, *extra = entry
        if len(name) > _CMD_NAME_LIMIT:
            candidate = name[:_CMD_NAME_LIMIT]
            if candidate in used:
                prefix = name[:_CMD_NAME_LIMIT - 1]
                for digit in range(10):
                    candidate = f"{prefix}{digit}"
                    if candidate not in used:
                        break
                else:
                    continue
            name = candidate
        if name in used:
            continue
        used.add(name)
        result.append((name, desc, *extra))
    return result


_clamp_telegram_names = _clamp_command_names


# ---------------------------------------------------------------------------
# Сбор плагинов/навыков для платформ шлюза
# ---------------------------------------------------------------------------

def _collect_gateway_skill_entries(
    platform: str,
    max_slots: int,
    reserved_names: set[str],
    desc_limit: int = 100,
    sanitize_name: "Callable[[str], str] | None" = None,
) -> tuple[list[tuple[str, str, str]], int]:
    """Собрать записи плагинов + навыков для платформы шлюза."""
    all_entries: list[tuple[str, str, str]] = []

    # --- Уровень 1: Команды слэш плагинов (никогда не обрезаются) -----------
    plugin_pairs: list[tuple[str, str]] = []
    try:
        from hermes_cli.plugins import get_plugin_commands
        plugin_cmds = get_plugin_commands()
        for cmd_name in sorted(plugin_cmds):
            name = sanitize_name(cmd_name) if sanitize_name else cmd_name
            if not name:
                continue
            desc = plugin_cmds[cmd_name].get("description", "Команда плагина")
            if len(desc) > desc_limit:
                desc = desc[:desc_limit - 3] + "..."
            plugin_pairs.append((name, desc))
    except Exception:
        pass

    plugin_pairs = _clamp_command_names(plugin_pairs, reserved_names)
    reserved_names.update(n for n, _ in plugin_pairs)
    for n, d in plugin_pairs:
        all_entries.append((n, d, ""))

    # --- Уровень 2: Встроенные команды навыков (обрезаются при лимите) ------
    _platform_disabled: set[str] = set()
    try:
        from agent.skill_utils import get_disabled_skill_names
        _platform_disabled = get_disabled_skill_names(platform=platform)
    except Exception:
        pass

    skill_triples: list[tuple[str, str, str]] = []
    try:
        from agent.skill_commands import get_skill_commands
        from tools.skills_tool import SKILLS_DIR
        from agent.skill_utils import get_external_skills_dirs
        _skills_dir = str(SKILLS_DIR.resolve())
        _hub_dir = str((SKILLS_DIR / ".hub").resolve()).rstrip("/") + "/"
        _allowed_prefixes = [_skills_dir.rstrip("/") + "/"]
        _allowed_prefixes.extend(
            str(d).rstrip("/") + "/" for d in get_external_skills_dirs()
        )
        skill_cmds = get_skill_commands()
        for cmd_key in sorted(skill_cmds):
            info = skill_cmds[cmd_key]
            skill_path = info.get("skill_md_path", "")
            if not skill_path:
                continue
            if not any(skill_path.startswith(prefix) for prefix in _allowed_prefixes):
                continue
            if skill_path.startswith(_hub_dir):
                continue
            skill_name = info.get("name", "")
            if skill_name in _platform_disabled:
                continue
            raw_name = cmd_key.lstrip("/")
            name = sanitize_name(raw_name) if sanitize_name else raw_name
            if not name:
                continue
            desc = info.get("description", "")
            if len(desc) > desc_limit:
                desc = desc[:desc_limit - 3] + "..."
            skill_triples.append((name, desc, cmd_key))
    except Exception:
        pass

    skill_triples = _clamp_command_names(skill_triples, reserved_names)

    remaining = max(0, max_slots - len(all_entries))
    hidden_count = max(0, len(skill_triples) - remaining)
    for n, d, k in skill_triples[:remaining]:
        all_entries.append((n, d, k))

    return all_entries[:max_slots], hidden_count


# ---------------------------------------------------------------------------
# Обертки для платформ
# ---------------------------------------------------------------------------

def telegram_menu_commands(max_commands: int = 100) -> tuple[list[tuple[str, str]], int]:
    """Возвращает команды меню Telegram, ограниченные лимитом Bot API."""
    core_commands = list(telegram_bot_commands())
    reserved_names = {n for n, _ in core_commands}
    all_commands = list(core_commands)

    remaining_slots = max(0, max_commands - len(all_commands))
    entries, hidden_count = _collect_gateway_skill_entries(
        platform="telegram",
        max_slots=remaining_slots,
        reserved_names=reserved_names,
        desc_limit=40,
        sanitize_name=_sanitize_telegram_name,
    )
    all_commands.extend((n, d) for n, d, _k in entries)
    return all_commands[:max_commands], hidden_count


def discord_skill_commands(
    max_slots: int,
    reserved_names: set[str],
) -> tuple[list[tuple[str, str, str]], int]:
    """Возвращает записи навыков для регистрации слэш-команд Discord."""
    return _collect_gateway_skill_entries(
        platform="discord",
        max_slots=max_slots,
        reserved_names=set(reserved_names),
        desc_limit=100,
    )


def discord_skill_commands_by_category(
    reserved_names: set[str],
) -> tuple[dict[str, list[tuple[str, str, str]]], list[tuple[str, str, str]], int]:
    """Возвращает записи навыков, организованные по категориям для Discord /skill."""
    from pathlib import Path as _P

    _platform_disabled: set[str] = set()
    try:
        from agent.skill_utils import get_disabled_skill_names
        _platform_disabled = get_disabled_skill_names(platform="discord")
    except Exception:
        pass

    categories: dict[str, list[tuple[str, str, str]]] = {}
    uncategorized: list[tuple[str, str, str]] = []
    _names_used: dict[str, str] = {n: "<reserved>" for n in reserved_names}
    hidden = 0

    try:
        from agent.skill_commands import get_skill_commands
        from agent.skill_utils import get_external_skills_dirs
        from tools.skills_tool import SKILLS_DIR

        _skills_dir = SKILLS_DIR.resolve()
        _hub_dir = (SKILLS_DIR / ".hub").resolve()
        _scan_roots: list[_P] = [_skills_dir]
        try:
            for ext in get_external_skills_dirs():
                try:
                    _scan_roots.append(_P(ext).resolve())
                except Exception:
                    continue
        except Exception:
            pass
        skill_cmds = get_skill_commands()

        for cmd_key in sorted(skill_cmds):
            info = skill_cmds[cmd_key]
            skill_path = info.get("skill_md_path", "")
            if not skill_path:
                continue
            sp = _P(skill_path).resolve()
            if str(sp).startswith(str(_hub_dir)):
                continue
            matched_root: _P | None = None
            for root in _scan_roots:
                try:
                    sp.relative_to(root)
                except ValueError:
                    continue
                matched_root = root
                break
            if matched_root is None:
                continue

            skill_name = info.get("name", "")
            if skill_name in _platform_disabled:
                continue

            raw_name = cmd_key.lstrip("/")
            discord_name = raw_name[:32]
            if discord_name in _names_used:
                hidden += 1
                continue
            _names_used[discord_name] = cmd_key

            desc = info.get("description", "")
            if len(desc) > 100:
                desc = desc[:97] + "..."

            rel = sp.parent.relative_to(matched_root)
            parts = rel.parts
            if len(parts) >= 2:
                cat = parts[0]
                categories.setdefault(cat, []).append((discord_name, desc, cmd_key))
            else:
                uncategorized.append((discord_name, desc, cmd_key))
    except Exception:
        pass

    return categories, uncategorized, hidden


# ---------------------------------------------------------------------------
# Нативные слэш-команды Slack
# ---------------------------------------------------------------------------

_SLACK_MAX_SLASH_COMMANDS = 50
_SLACK_NAME_LIMIT = 32
_SLACK_INVALID_CHARS = re.compile(r"[^a-z0-9_\-]")
_SLACK_RESERVED_COMMANDS = frozenset({
    "me", "status", "away", "dnd", "shrug", "remind", "msg", "feed",
    "who", "collapse", "expand", "leave", "join", "open", "search",
    "topic", "mute", "pro", "shortcuts",
})


def _sanitize_slack_name(raw: str) -> str:
    """Преобразовать имя команды в валидное имя слэш-команды Slack."""
    name = raw.lower()
    name = _SLACK_INVALID_CHARS.sub("", name)
    name = name.strip("-_")
    return name[:_SLACK_NAME_LIMIT]


def slack_native_slashes() -> list[tuple[str, str, str]]:
    """Возвращает тройки (slash_name, description, usage_hint) для Slack."""
    overrides = _resolve_config_gates()
    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    entries.append(("hermes", "Поговорить с Hermes или запустить подкоманду", "[подкоманда] [аргументы]"))
    seen.add("hermes")

    def _add(name: str, desc: str, hint: str) -> None:
        slack_name = _sanitize_slack_name(name)
        if not slack_name or slack_name in seen:
            return
        if slack_name in _SLACK_RESERVED_COMMANDS:
            return
        if len(entries) >= _SLACK_MAX_SLASH_COMMANDS:
            return
        entries.append((slack_name, desc[:140], hint[:100]))
        seen.add(slack_name)

    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        _add(cmd.name, cmd.description, cmd.args_hint or "")

    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        for alias in cmd.aliases:
            _add(alias, f"Алиас для /{cmd.name} — {cmd.description}", cmd.args_hint or "")

    for name, description, args_hint in _iter_plugin_command_entries():
        _add(name, description, args_hint or "")

    return entries


def slack_app_manifest(request_url: str = "https://hermes-agent.local/slack/commands") -> dict[str, Any]:
    """Сгенерировать манифест приложения Slack со всеми командами шлюза."""
    slashes = []
    for name, desc, usage in slack_native_slashes():
        entry = {
            "command": f"/{name}",
            "description": desc or f"Запустить /{name}",
            "should_escape": False,
            "url": request_url,
        }
        if usage:
            entry["usage_hint"] = usage
        slashes.append(entry)
    return {"features": {"slash_commands": slashes}}


def slack_subcommand_map() -> dict[str, str]:
    """Возвращает маппинг подкоманда -> /команда для обработчика Slack /hermes."""
    overrides = _resolve_config_gates()
    mapping: dict[str, str] = {}
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        mapping[cmd.name] = f"/{cmd.name}"
        for alias in cmd.aliases:
            mapping[alias] = f"/{alias}"
    for name, _description, _args_hint in _iter_plugin_command_entries():
        if name not in mapping:
            mapping[name] = f"/{name}"
    return mapping


# ---------------------------------------------------------------------------
# Автодополнение
# ---------------------------------------------------------------------------

_LMSTUDIO_COMPLETION_CACHE: tuple[float, list[str]] | None = None


def _lmstudio_completion_models() -> list[str]:
    """Локально загруженные модели LM Studio для автодополнения /model."""
    global _LMSTUDIO_COMPLETION_CACHE
    if not (os.environ.get("LM_API_KEY") or os.environ.get("LM_BASE_URL")):
        try:
            from hermes_cli.auth import _load_auth_store
            store = _load_auth_store() or {}
            if "lmstudio" not in (store.get("providers") or {}) \
               and "lmstudio" not in (store.get("credential_pool") or {}):
                return []
        except Exception:
            return []
    now = time.time()
    if _LMSTUDIO_COMPLETION_CACHE and (now - _LMSTUDIO_COMPLETION_CACHE[0]) < 30.0:
        return _LMSTUDIO_COMPLETION_CACHE[1]
    try:
        from hermes_cli.models import fetch_lmstudio_models
        models = fetch_lmstudio_models(
            api_key=os.environ.get("LM_API_KEY", ""),
            base_url=os.environ.get("LM_BASE_URL") or "http://127.0.0.1:1234/v1",
            timeout=0.8,
        )
    except Exception:
        models = []
    _LMSTUDIO_COMPLETION_CACHE = (now, models)
    return models


class SlashCommandCompleter(Completer):
    """Автодополнение для встроенных слэш-команд, подкоманд и команд навыков."""

    def __init__(
        self,
        skill_commands_provider: Callable[[], Mapping[str, dict[str, Any]]] | None = None,
        command_filter: Callable[[str], bool] | None = None,
    ) -> None:
        self._skill_commands_provider = skill_commands_provider
        self._command_filter = command_filter
        self._file_cache: list[str] = []
        self._file_cache_time: float = 0.0
        self._file_cache_cwd: str = ""

    def _command_allowed(self, slash_command: str) -> bool:
        if self._command_filter is None:
            return True
        try:
            return bool(self._command_filter(slash_command))
        except Exception:
            return True

    def _iter_skill_commands(self) -> Mapping[str, dict[str, Any]]:
        if self._skill_commands_provider is None:
            return {}
        try:
            return self._skill_commands_provider() or {}
        except Exception:
            return {}

    _PICKER_COMMANDS = frozenset({"model", "skin", "personality"})

    @staticmethod
    def _completion_text(cmd_name: str, word: str) -> str:
        """Возвращает замещающий текст для автодополнения."""
        if cmd_name != word:
            return cmd_name
        if cmd_name in SlashCommandCompleter._PICKER_COMMANDS:
            return cmd_name
        return f"{cmd_name} "

    @staticmethod
    def _extract_path_word(text: str) -> str | None:
        """Извлечь текущее слово, если оно похоже на путь к файлу."""
        if not text:
            return None
        i = len(text) - 1
        while i >= 0 and text[i] != " ":
            i -= 1
        word = text[i + 1:]
        if not word:
            return None
        if word.startswith(("./", "../", "~/", "/")) or "/" in word:
            return word
        return None

    @staticmethod
    def _path_completions(word: str, limit: int = 30):
        """Выдает объекты Completion для путей к файлам, соответствующих *word*."""
        expanded = os.path.expanduser(word)
        if expanded.endswith("/"):
            search_dir = expanded
            prefix = ""
        else:
            search_dir = os.path.dirname(expanded) or "."
            prefix = os.path.basename(expanded)

        try:
            entries = os.listdir(search_dir)
        except OSError:
            return

        count = 0
        prefix_lower = prefix.lower()
        for entry in sorted(entries):
            if prefix and not entry.lower().startswith(prefix_lower):
                continue
            if count >= limit:
                break

            full_path = os.path.join(search_dir, entry)
            is_dir = os.path.isdir(full_path)

            if word.startswith("~"):
                display_path = "~/" + os.path.relpath(full_path, os.path.expanduser("~"))
            elif os.path.isabs(word):
                display_path = full_path
            else:
                display_path = os.path.relpath(full_path)

            if is_dir:
                display_path += "/"

            suffix = "/" if is_dir else ""
            meta = "дир" if is_dir else _file_size_label(full_path)

            yield Completion(
                display_path,
                start_position=-len(word),
                display=entry + suffix,
                display_meta=meta,
            )
            count += 1

    @staticmethod
    def _extract_context_word(text: str) -> str | None:
        """Извлечь токен ``@`` для автодополнения ссылок на контекст."""
        if not text:
            return None
        i = len(text) - 1
        while i >= 0 and text[i] != " ":
            i -= 1
        word = text[i + 1:]
        if not word.startswith("@"):
            return None
        return word

    def _context_completions(self, word: str, limit: int = 30):
        """Выдает автодополнения контекста в стиле Claude Code."""
        lowered = word.lower()

        _STATIC_REFS = (
            ("@diff", "Diff рабочей области Git"),
            ("@staged", "Staged diff Git"),
            ("@file:", "Прикрепить файл"),
            ("@folder:", "Прикрепить папку"),
            ("@git:", "Лог Git с диффами (напр. @git:5)"),
            ("@url:", "Загрузить веб-контент"),
        )
        for candidate, meta in _STATIC_REFS:
            if candidate.lower().startswith(lowered) and candidate.lower() != lowered:
                yield Completion(
                    candidate,
                    start_position=-len(word),
                    display=candidate,
                    display_meta=meta,
                )

        for prefix in ("@file:", "@folder:"):
            bare = prefix[:-1]

            if word == bare or word.startswith(prefix):
                want_dir = prefix == "@folder:"
                path_part = '' if word == bare else word[len(prefix):]
                expanded = os.path.expanduser(path_part)

                if not expanded or expanded == ".":
                    search_dir, match_prefix = ".", ""
                elif expanded.endswith("/"):
                    search_dir, match_prefix = expanded, ""
                else:
                    search_dir = os.path.dirname(expanded) or "."
                    match_prefix = os.path.basename(expanded)

                try:
                    entries = os.listdir(search_dir)
                except OSError:
                    return

                count = 0
                prefix_lower = match_prefix.lower()
                for entry in sorted(entries):
                    if match_prefix and not entry.lower().startswith(prefix_lower):
                        continue
                    full_path = os.path.join(search_dir, entry)
                    is_dir = os.path.isdir(full_path)
                    if want_dir != is_dir:
                        continue
                    if count >= limit:
                        break
                    display_path = os.path.relpath(full_path)
                    suffix = "/" if is_dir else ""
                    meta = "дир" if is_dir else _file_size_label(full_path)
                    completion = f"{prefix}{display_path}{suffix}"
                    yield Completion(
                        completion,
                        start_position=-len(word),
                        display=entry + suffix,
                        display_meta=meta,
                    )
                    count += 1
                return

        query = word[1:]
        yield from self._fuzzy_file_completions(word, query, limit)

    def _get_project_files(self) -> list[str]:
        """Возвращает кэшированный список файлов проекта (обновляется каждые 5с)."""
        cwd = os.getcwd()
        now = time.monotonic()
        if (
            self._file_cache
            and self._file_cache_cwd == cwd
            and now - self._file_cache_time < 5.0
        ):
            return self._file_cache

        files: list[str] = []
        for cmd in [
            ["rg", "--files", "--sortr=modified", cwd],
            ["rg", "--files", cwd],
            ["fd", "--type", "f", "--base-directory", cwd],
        ]:
            tool = cmd[0]
            if not shutil.which(tool):
                continue
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=2,
                    cwd=cwd,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    raw = proc.stdout.strip().split("\n")
                    for p in raw[:5000]:
                        rel = os.path.relpath(p, cwd) if os.path.isabs(p) else p
                        files.append(rel)
                    break
            except (subprocess.TimeoutExpired, OSError):
                continue

        self._file_cache = files
        self._file_cache_time = now
        self._file_cache_cwd = cwd
        return files

    @staticmethod
    def _score_path(filepath: str, query: str) -> int:
        """Оценивает путь к файлу по нечеткому запросу. Чем выше, тем лучше соответствие."""
        if not query:
            return 1

        filename = os.path.basename(filepath)
        lower_file = filename.lower()
        lower_path = filepath.lower()
        lower_q = query.lower()

        if lower_file == lower_q:
            return 100
        if lower_file.startswith(lower_q):
            return 80
        if lower_q in lower_file:
            return 60
        if lower_q in lower_path:
            return 40
        qi = 0
        for c in lower_file:
            if qi < len(lower_q) and c == lower_q[qi]:
                qi += 1
        if qi == len(lower_q):
            boundary_hits = 0
            qi = 0
            prev = "_"
            for c in lower_file:
                if qi < len(lower_q) and c == lower_q[qi]:
                    if prev in "_-./":
                        boundary_hits += 1
                    qi += 1
                prev = c
            if boundary_hits >= len(lower_q) * 0.5:
                return 35
            return 25
        return 0

    def _fuzzy_file_completions(self, word: str, query: str, limit: int = 20):
        """Выдает нечеткие автодополнения файлов для bare @query."""
        files = self._get_project_files()

        if not query:
            for fp in files[:limit]:
                is_dir = fp.endswith("/")
                filename = os.path.basename(fp)
                kind = "папка" if is_dir else "файл"
                meta = "дир" if is_dir else _file_size_label(
                    os.path.join(os.getcwd(), fp)
                )
                yield Completion(
                    f"@{kind}:{fp}",
                    start_position=-len(word),
                    display=filename,
                    display_meta=meta,
                )
            return

        scored = []
        for fp in files:
            s = self._score_path(fp, query)
            if s > 0:
                scored.append((s, fp))
        scored.sort(key=lambda x: (-x[0], x[1]))

        for _, fp in scored[:limit]:
            is_dir = fp.endswith("/")
            filename = os.path.basename(fp)
            kind = "папка" if is_dir else "файл"
            meta = "дир" if is_dir else _file_size_label(
                os.path.join(os.getcwd(), fp)
            )
            yield Completion(
                f"@{kind}:{fp}",
                start_position=-len(word),
                display=filename,
                display_meta=f"{fp}  {meta}" if meta else fp,
            )

    @staticmethod
    def _skin_completions(sub_text: str, sub_lower: str):
        """Выдает автодополнения для /skin из доступных скинов."""
        try:
            from hermes_cli.skin_engine import list_skins
            for s in list_skins():
                name = s["name"]
                if name.startswith(sub_lower) and name != sub_lower:
                    yield Completion(
                        name,
                        start_position=-len(sub_text),
                        display=name,
                        display_meta=s.get("description", "") or s.get("source", ""),
                    )
        except Exception:
            pass

    @staticmethod
    def _personality_completions(sub_text: str, sub_lower: str):
        """Выдает автодополнения для /personality из настроенных личностей."""
        try:
            from hermes_cli.config import load_config
            personalities = load_config().get("agent", {}).get("personalities", {})
            if "none".startswith(sub_lower) and "none" != sub_lower:
                yield Completion(
                    "none",
                    start_position=-len(sub_text),
                    display="none",
                    display_meta="очистить наложение личности",
                )
            for name, prompt in personalities.items():
                if name.startswith(sub_lower) and name != sub_lower:
                    if isinstance(prompt, dict):
                        meta = prompt.get("description") or prompt.get("system_prompt", "")[:50]
                    else:
                        meta = str(prompt)[:50]
                    yield Completion(
                        name,
                        start_position=-len(sub_text),
                        display=name,
                        display_meta=meta,
                    )
        except Exception:
            pass

    def _model_completions(self, sub_text: str, sub_lower: str):
        """Выдает автодополнения для /model из алиасов конфига + встроенных алиасов."""
        seen = set()
        try:
            from hermes_cli.model_switch import (
                _ensure_direct_aliases, DIRECT_ALIASES, MODEL_ALIASES,
            )
            _ensure_direct_aliases()
            for name, da in DIRECT_ALIASES.items():
                if name.startswith(sub_lower) and name != sub_lower:
                    seen.add(name)
                    yield Completion(
                        name,
                        start_position=-len(sub_text),
                        display=name,
                        display_meta=f"{da.model} ({da.provider})",
                    )
            for name in sorted(MODEL_ALIASES.keys()):
                if name in seen:
                    continue
                if name.startswith(sub_lower) and name != sub_lower:
                    identity = MODEL_ALIASES[name]
                    yield Completion(
                        name,
                        start_position=-len(sub_text),
                        display=name,
                        display_meta=f"{identity.vendor}/{identity.family}",
                    )
        except Exception:
            pass
        for name in _lmstudio_completion_models():
            if name in seen:
                continue
            if name.startswith(sub_lower) and name != sub_lower:
                yield Completion(
                    name,
                    start_position=-len(sub_text),
                    display=name,
                    display_meta="LM Studio",
                )

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            ctx_word = self._extract_context_word(text)
            if ctx_word is not None:
                yield from self._context_completions(ctx_word)
                return
            path_word = self._extract_path_word(text)
            if path_word is not None:
                yield from self._path_completions(path_word)
            return

        parts = text.split(maxsplit=1)
        base_cmd = parts[0].lower()
        if len(parts) > 1 or (len(parts) == 1 and text.endswith(" ")):
            sub_text = parts[1] if len(parts) > 1 else ""
            sub_lower = sub_text.lower()

            if " " not in sub_text:
                if base_cmd == "/model":
                    yield from self._model_completions(sub_text, sub_lower)
                    return
                if base_cmd == "/skin":
                    yield from self._skin_completions(sub_text, sub_lower)
                    return
                if base_cmd == "/personality":
                    yield from self._personality_completions(sub_text, sub_lower)
                    return

            if " " not in sub_text and base_cmd in SUBCOMMANDS and self._command_allowed(base_cmd):
                for sub in SUBCOMMANDS[base_cmd]:
                    if sub.startswith(sub_lower) and sub != sub_lower:
                        yield Completion(
                            sub,
                            start_position=-len(sub_text),
                            display=sub,
                        )
            return

        word = text[1:]

        for cmd, desc in COMMANDS.items():
            if not self._command_allowed(cmd):
                continue
            cmd_name = cmd[1:]
            if cmd_name.startswith(word):
                yield Completion(
                    self._completion_text(cmd_name, word),
                    start_position=-len(word),
                    display=cmd,
                    display_meta=desc,
                )

        for cmd, info in self._iter_skill_commands().items():
            cmd_name = cmd[1:]
            if cmd_name.startswith(word):
                description = str(info.get("description", "Команда навыка"))
                short_desc = description[:50] + ("..." if len(description) > 50 else "")
                yield Completion(
                    self._completion_text(cmd_name, word),
                    start_position=-len(word),
                    display=cmd,
                    display_meta=f"⚡ {short_desc}",
                )

        try:
            from hermes_cli.plugins import get_plugin_commands
            for cmd_name, cmd_info in get_plugin_commands().items():
                if cmd_name.startswith(word):
                    desc = str(cmd_info.get("description", "Команда плагина"))
                    short_desc = desc[:50] + ("..." if len(desc) > 50 else "")
                    yield Completion(
                        self._completion_text(cmd_name, word),
                        start_position=-len(word),
                        display=f"/{cmd_name}",
                        display_meta=f"🔌 {short_desc}",
                    )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Встроенный автосаггест (ghost text) для слэш-команд
# ---------------------------------------------------------------------------

class SlashCommandAutoSuggest(AutoSuggest):
    """Предложения ghost-text для слэш-команд и их подкоманд."""

    def __init__(
        self,
        history_suggest: AutoSuggest | None = None,
        completer: SlashCommandCompleter | None = None,
    ) -> None:
        self._history = history_suggest
        self._completer = completer

    def get_suggestion(self, buffer, document):
        text = document.text_before_cursor

        if not text.startswith("/"):
            if self._history:
                return self._history.get_suggestion(buffer, document)
            return None

        parts = text.split(maxsplit=1)
        base_cmd = parts[0].lower()

        if len(parts) == 1 and not text.endswith(" "):
            word = text[1:].lower()
            for cmd in COMMANDS:
                if self._completer is not None and not self._completer._command_allowed(cmd):
                    continue
                cmd_name = cmd[1:]
                if cmd_name.startswith(word) and cmd_name != word:
                    return Suggestion(cmd_name[len(word):])
            return None

        sub_text = parts[1] if len(parts) > 1 else ""
        sub_lower = sub_text.lower()

        if self._completer is not None and not self._completer._command_allowed(base_cmd):
            return None
        if base_cmd in SUBCOMMANDS and SUBCOMMANDS[base_cmd]:
            if " " not in sub_text:
                for sub in SUBCOMMANDS[base_cmd]:
                    if sub.startswith(sub_lower) and sub != sub_lower:
                        return Suggestion(sub[len(sub_text):])

        if self._history:
            return self._history.get_suggestion(buffer, document)
        return None


def _file_size_label(path: str) -> str:
    """Возвращает компактный человекочитаемый размер файла."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    if size < 1024:
        return f"{size}Б"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f}К"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f}М"
    return f"{size / (1024 * 1024 * 1024):.1f}Г"
