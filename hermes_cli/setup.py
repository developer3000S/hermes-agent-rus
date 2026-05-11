"""
Интерактивный мастер настройки Hermes Agent.

Модульный мастер с независимыми разделами:
  1. Модель и провайдер — выбор AI-провайдера и модели
  2. Терминальный бэкенд — где агент выполняет команды
  3. Настройки агента — итерации, сжатие, сброс сессии
  4. Платформы обмена сообщениями — подключение Telegram, Discord и др.
  5. Инструменты — настройка TTS, веб-поиска, генерации изображений и т.д.

Конфигурационные файлы хранятся в ~/.hermes/ для удобного доступа.
"""

import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import copy
from pathlib import Path
from typing import Optional, Dict, Any

from hermes_cli.nous_subscription import get_nous_subscription_features
from tools.tool_backend_helpers import managed_nous_tools_enabled
from utils import base_url_hostname
from hermes_constants import get_optional_skills_dir

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

_DOCS_BASE = "https://hermes-agent.nousresearch.com/docs"


def _model_config_dict(config: Dict[str, Any]) -> Dict[str, Any]:
    current_model = config.get("model")
    if isinstance(current_model, dict):
        return dict(current_model)
    if isinstance(current_model, str) and current_model.strip():
        return {"default": current_model.strip()}
    return {}


def _get_credential_pool_strategies(config: Dict[str, Any]) -> Dict[str, str]:
    strategies = config.get("credential_pool_strategies")
    return dict(strategies) if isinstance(strategies, dict) else {}


def _set_credential_pool_strategy(config: Dict[str, Any], provider: str, strategy: str) -> None:
    if not provider:
        return
    strategies = _get_credential_pool_strategies(config)
    strategies[provider] = strategy
    config["credential_pool_strategies"] = strategies


def _supports_same_provider_pool_setup(provider: str) -> bool:
    if not provider or provider == "custom":
        return False
    if provider == "openrouter":
        return True
    from hermes_cli.auth import PROVIDER_REGISTRY

    pconfig = PROVIDER_REGISTRY.get(provider)
    if not pconfig:
        return False
    return pconfig.auth_type in {"api_key", "oauth_device_code"}


# Списки моделей по умолчанию для каждого провайдера — используются как резервные,
# когда эндпоинт /models недоступен.
_DEFAULT_PROVIDER_MODELS = {
    "copilot-acp": [
        "copilot-acp",
    ],
    "copilot": [
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5-mini",
        "gpt-5.3-codex",
        "gpt-5.2-codex",
        "gpt-4.1",
        "gpt-4o",
        "gpt-4o-mini",
        "claude-opus-4.6",
        "claude-sonnet-4.6",
        "claude-sonnet-4.5",
        "claude-haiku-4.5",
        "gemini-2.5-pro",
    ],
    "gemini": [
        "gemini-3.1-pro-preview", "gemini-3-pro-preview",
        "gemini-3-flash-preview", "gemini-3.1-flash-lite-preview",
    ],
    "zai": ["glm-5.1", "glm-5", "glm-4.7", "glm-4.5", "glm-4.5-flash"],
    "kimi-coding": ["kimi-k2.6", "kimi-k2.5", "kimi-k2-thinking", "kimi-k2-turbo-preview"],
    "kimi-coding-cn": ["kimi-k2.6", "kimi-k2.5", "kimi-k2-thinking", "kimi-k2-turbo-preview"],
    "stepfun": ["step-3.5-flash", "step-3.5-flash-2603"],
    "arcee": ["trinity-large-thinking", "trinity-large-preview", "trinity-mini"],
    "minimax": ["MiniMax-M2.7", "MiniMax-M2.5", "MiniMax-M2.1", "MiniMax-M2"],
    "minimax-cn": ["MiniMax-M2.7", "MiniMax-M2.5", "MiniMax-M2.1", "MiniMax-M2"],
    "ai-gateway": ["anthropic/claude-opus-4.6", "anthropic/claude-sonnet-4.6", "openai/gpt-5", "google/gemini-3-flash"],
    "kilocode": ["anthropic/claude-opus-4.6", "anthropic/claude-sonnet-4.6", "openai/gpt-5.4", "google/gemini-3-pro-preview", "google/gemini-3-flash-preview"],
    "opencode-zen": ["gpt-5.4", "gpt-5.3-codex", "claude-sonnet-4-6", "gemini-3-flash", "glm-5", "kimi-k2.5", "minimax-m2.7"],
    "opencode-go": ["kimi-k2.6", "kimi-k2.5", "glm-5.1", "glm-5", "mimo-v2.5-pro", "mimo-v2.5", "mimo-v2-pro", "mimo-v2-omni", "minimax-m2.7", "minimax-m2.5", "qwen3.6-plus", "qwen3.5-plus"],
    "huggingface": [
        "Qwen/Qwen3.5-397B-A17B", "Qwen/Qwen3-235B-A22B-Thinking-2507",
        "Qwen/Qwen3-Coder-480B-A35B-Instruct", "deepseek-ai/DeepSeek-R1-0528",
        "deepseek-ai/DeepSeek-V3.2", "moonshotai/Kimi-K2.5",
    ],
}


def _current_reasoning_effort(config: Dict[str, Any]) -> str:
    agent_cfg = config.get("agent")
    if isinstance(agent_cfg, dict):
        return str(agent_cfg.get("reasoning_effort") or "").strip().lower()
    return ""


def _set_reasoning_effort(config: Dict[str, Any], effort: str) -> None:
    agent_cfg = config.get("agent")
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
        config["agent"] = agent_cfg
    agent_cfg["reasoning_effort"] = effort




# Импорт вспомогательных функций для работы с конфигурацией
from hermes_cli.config import (
    cfg_get,
    DEFAULT_CONFIG,
    get_hermes_home,
    get_config_path,
    get_env_path,
    load_config,
    save_config,
    save_env_value,
    remove_env_value,
    get_env_value,
    ensure_hermes_home,
)
# display_hermes_home импортируется лениво в местах вызова (безопасность при обновлении hermes)

from hermes_cli.colors import Colors, color


def print_header(title: str):
    """Печать заголовка раздела."""
    print()
    print(color(f"◆ {title}", Colors.CYAN, Colors.BOLD))


from hermes_cli.cli_output import (  # noqa: E402
    print_error,
    print_info,
    print_success,
    print_warning,
)


def is_interactive_stdin() -> bool:
    """Возвращает True, если stdin выглядит как используемый интерактивный TTY."""
    stdin = getattr(sys, "stdin", None)
    if stdin is None:
        return False
    try:
        return bool(stdin.isatty())
    except Exception:
        return False


def print_noninteractive_setup_guidance(reason: str | None = None) -> None:
    """Вывод инструкций для неинтерактивных потоков настройки."""
    print()
    print(color("⚕ Настройка Hermes — Неинтерактивный режим", Colors.CYAN, Colors.BOLD))
    print()
    if reason:
        print_info(reason)
    print_info("Интерактивный мастер здесь не может быть использован.")
    print()
    print_info("Настройте Hermes с помощью переменных окружения или команд конфигурации:")
    print_info("  hermes config set model.provider custom")
    print_info("  hermes config set model.base_url http://localhost:8080/v1")
    print_info("  hermes config set model.default your-model-name")
    print()
    print_info("Или установите OPENROUTER_API_KEY / OPENAI_API_KEY в вашем окружении.")
    print_info("Запустите 'hermes setup' в интерактивном терминале для использования полного мастера.")
    print()


def prompt(question: str, default: str = None, password: bool = False) -> str:
    """Запрос ввода с необязательным значением по умолчанию."""
    if default:
        display = f"{question} [{default}]: "
    else:
        display = f"{question}: "

    try:
        if password:
            import getpass

            value = getpass.getpass(color(display, Colors.YELLOW))
        else:
            value = input(color(display, Colors.YELLOW))

        cleaned = _sanitize_pasted_input(value)
        return cleaned.strip() or default or ""
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(1)


_BRACKETED_PASTE_PATTERN = re.compile(r"\x1b\[\s*200~|\x1b\[\s*201~")


def _sanitize_pasted_input(value: str) -> str:
    """Удаление управляющих маркеров bracketed-paste из вставленного текста."""
    if not isinstance(value, str) or not value:
        return value
    return _BRACKETED_PASTE_PATTERN.sub("", value)


def _curses_prompt_choice(question: str, choices: list, default: int = 0, description: str | None = None) -> int:
    """Меню выбора с использованием curses. Делегирует выполнение curses_radiolist."""
    from hermes_cli.curses_ui import curses_radiolist
    return curses_radiolist(question, choices, selected=default, cancel_returns=-1, description=description)



def prompt_choice(question: str, choices: list, default: int = 0, description: str | None = None) -> int:
    """Запрос выбора из списка с навигацией стрелками.

    Escape сохраняет текущее значение (пропускает вопрос).
    Ctrl+C завершает работу мастера.
    """
    idx = _curses_prompt_choice(question, choices, default, description=description)
    if idx >= 0:
        if idx == default:
            print_info("  Пропущено (сохранение текущего)")
            print()
            return default
        print()
        return idx

    print(color(question, Colors.YELLOW))
    for i, choice in enumerate(choices):
        marker = "●" if i == default else "○"
        if i == default:
            print(color(f"  {marker} {choice}", Colors.GREEN))
        else:
            print(f"  {marker} {choice}")

    print_info(f"  Enter — значение по умолчанию ({default + 1})  Ctrl+C — выход")

    while True:
        try:
            value = input(
                color(f"  Выберите [1-{len(choices)}] ({default + 1}): ", Colors.DIM)
            )
            if not value:
                return default
            idx = int(value) - 1
            if 0 <= idx < len(choices):
                return idx
            print_error(f"Пожалуйста, введите число от 1 до {len(choices)}")
        except ValueError:
            print_error("Пожалуйста, введите число")
        except (KeyboardInterrupt, EOFError):
            print()
            sys.exit(1)


def prompt_yes_no(question: str, default: bool = True) -> bool:
    """Запрос да/нет. Ctrl+C — выход, пустой ввод возвращает значение по умолчанию."""
    default_str = "Y/n" if default else "y/N"

    while True:
        try:
            value = (
                input(color(f"{question} [{default_str}]: ", Colors.YELLOW))
                .strip()
                .lower()
            )
        except (KeyboardInterrupt, EOFError):
            print()
            sys.exit(1)

        if not value:
            return default
        if value in ("y", "yes"):
            return True
        if value in ("n", "no"):
            return False
        print_error("Пожалуйста, введите 'y' или 'n'")


def prompt_checklist(title: str, items: list, pre_selected: list = None) -> list:
    """
    Отображение контрольного списка с множественным выбором и возврат индексов выбранных элементов.

    Каждый элемент в `items` — это отображаемая строка. `pre_selected` — список
    индексов, которые должны быть отмечены по умолчанию. Опция "Продолжить →"
    добавляется в конце — пользователь переключает элементы пробелом и подтверждает
    выбор клавишей Enter на "Продолжить →".

    Возвращается к интерфейсу с нумерованными переключателями, если simple_term_menu
    недоступен.

    Возвращает:
        Список выбранных индексов (не включая опцию Продолжить).
    """
    if pre_selected is None:
        pre_selected = []

    from hermes_cli.curses_ui import curses_checklist

    chosen = curses_checklist(
        title,
        items,
        set(pre_selected),
        cancel_returns=set(pre_selected),
    )
    return sorted(chosen)


def _prompt_api_key(var: dict):
    """Отображение красиво отформатированного экрана ввода API-ключа для одной переменной окружения."""
    tools = var.get("tools", [])
    tools_str = ", ".join(tools[:3])
    if len(tools) > 3:
        tools_str += f", +{len(tools) - 3} еще"

    print()
    print(color(f"  ─── {var.get('description', var['name'])} ───", Colors.CYAN))
    print()
    if tools_str:
        print_info(f"  Включает: {tools_str}")
    if var.get("url"):
        print_info(f"  Получите ваш ключ здесь: {var['url']}")
    print()

    if var.get("password"):
        value = prompt(f"  {var.get('prompt', var['name'])}", password=True)
    else:
        value = prompt(f"  {var.get('prompt', var['name'])}")

    if value:
        save_env_value(var["name"], value)
        print_success("  ✓ Сохранено")
    else:
        print_warning("  Пропущено (настройте позже с помощью 'hermes setup')")


def _print_setup_summary(config: dict, hermes_home):
    """Печать сводки о завершении настройки."""
    # Сводка о доступности инструментов
    print()
    print_header("Сводка доступности инструментов")

    tool_status = []
    subscription_features = get_nous_subscription_features(config)

    # Vision — использование того же распознавателя среды выполнения, что и в реальных инструментах vision
    try:
        from agent.auxiliary_client import get_available_vision_backends

        _vision_backends = get_available_vision_backends()
    except Exception:
        _vision_backends = []

    if _vision_backends:
        tool_status.append(("Vision (анализ изображений)", True, None))
    else:
        tool_status.append(("Vision (анализ изображений)", False, "запустите 'hermes setup' для настройки"))

    # Mixture of Agents — требует OpenRouter (вызывает несколько моделей)
    if get_env_value("OPENROUTER_API_KEY"):
        tool_status.append(("Mixture of Agents", True, None))
    else:
        tool_status.append(("Mixture of Agents", False, "OPENROUTER_API_KEY"))

    # Веб-инструменты (Exa, Parallel, Firecrawl или Tavily)
    if subscription_features.web.managed_by_nous:
        tool_status.append(("Веб-поиск и извлечение (подписка Nous)", True, None))
    elif subscription_features.web.available:
        label = "Веб-поиск и извлечение"
        if subscription_features.web.current_provider:
            label = f"Веб-поиск и извлечение ({subscription_features.web.current_provider})"
        tool_status.append((label, True, None))
    else:
        tool_status.append(("Веб-поиск и извлечение", False, "EXA_API_KEY, PARALLEL_API_KEY, FIRECRAWL_API_KEY/FIRECRAWL_API_URL, TAVILY_API_KEY или SEARXNG_URL"))

    # Инструменты браузера (локальный Chromium, Camofox, Browserbase, Browser Use или Firecrawl)
    browser_provider = subscription_features.browser.current_provider
    if subscription_features.browser.managed_by_nous:
        tool_status.append(("Автоматизация браузера (Nous Browser Use)", True, None))
    elif subscription_features.browser.available:
        label = "Автоматизация браузера"
        if browser_provider:
            label = f"Автоматизация браузера ({browser_provider})"
        tool_status.append((label, True, None))
    else:
        missing_browser_hint = "npm install -g agent-browser, установите CAMOFOX_URL или настройте Browser Use или Browserbase"
        if browser_provider == "Browserbase":
            missing_browser_hint = (
                "npm install -g agent-browser и установите "
                "BROWSERBASE_API_KEY/BROWSERBASE_PROJECT_ID"
            )
        elif browser_provider == "Browser Use":
            missing_browser_hint = (
                "npm install -g agent-browser и установите BROWSER_USE_API_KEY"
            )
        elif browser_provider == "Camofox":
            missing_browser_hint = "CAMOFOX_URL"
        elif browser_provider == "Local browser":
            missing_browser_hint = "npm install -g agent-browser"
        tool_status.append(
            ("Автоматизация браузера", False, missing_browser_hint)
        )

    # Генерация изображений — FAL (напрямую или через Nous) или любой зарегистрированный плагином
    # провайдер (OpenAI и т.д.)
    if subscription_features.image_gen.managed_by_nous:
        tool_status.append(("Генерация изображений (подписка Nous)", True, None))
    elif subscription_features.image_gen.available:
        tool_status.append(("Генерация изображений", True, None))
    else:
        # Поиск среди провайдеров, зарегистрированных плагинами, чтобы настройки только с OpenAI
        # не показывались как "отсутствует FAL_KEY".
        _img_backend = None
        try:
            from agent.image_gen_registry import list_providers
            from hermes_cli.plugins import _ensure_plugins_discovered

            _ensure_plugins_discovered()
            for _p in list_providers():
                if _p.name == "fal":
                    continue
                try:
                    if _p.is_available():
                        _img_backend = _p.display_name
                        break
                except Exception:
                    continue
        except Exception:
            pass
        if _img_backend:
            tool_status.append((f"Генерация изображений ({_img_backend})", True, None))
        else:
            tool_status.append(("Генерация изображений", False, "FAL_KEY или OPENAI_API_KEY"))

    # TTS — отображение настроенного провайдера
    tts_provider = cfg_get(config, "tts", "provider", default="edge")
    if subscription_features.tts.managed_by_nous:
        tool_status.append(("Синтез речи (OpenAI через подписку Nous)", True, None))
    elif tts_provider == "elevenlabs" and get_env_value("ELEVENLABS_API_KEY"):
        tool_status.append(("Синтез речи (ElevenLabs)", True, None))
    elif tts_provider == "openai" and (
        get_env_value("VOICE_TOOLS_OPENAI_KEY") or get_env_value("OPENAI_API_KEY")
    ):
        tool_status.append(("Синтез речи (OpenAI)", True, None))
    elif tts_provider == "minimax" and get_env_value("MINIMAX_API_KEY"):
        tool_status.append(("Синтез речи (MiniMax)", True, None))
    elif tts_provider == "mistral" and get_env_value("MISTRAL_API_KEY"):
        tool_status.append(("Синтез речи (Mistral Voxtral)", True, None))
    elif tts_provider == "gemini" and (get_env_value("GEMINI_API_KEY") or get_env_value("GOOGLE_API_KEY")):
        tool_status.append(("Синтез речи (Google Gemini)", True, None))
    elif tts_provider == "neutts":
        try:
            neutts_ok = importlib.util.find_spec("neutts") is not None
        except Exception:
            neutts_ok = False
        if neutts_ok:
            tool_status.append(("Синтез речи (NeuTTS локально)", True, None))
        else:
            tool_status.append(("Синтез речи (NeuTTS — не установлен)", False, "запустите 'hermes setup tts'"))
    elif tts_provider == "kittentts":
        try:
            import importlib.util
            kittentts_ok = importlib.util.find_spec("kittentts") is not None
        except Exception:
            kittentts_ok = False
        if kittentts_ok:
            tool_status.append(("Синтез речи (KittenTTS локально)", True, None))
        else:
            tool_status.append(("Синтез речи (KittenTTS — не установлен)", False, "запустите 'hermes setup tts'"))
    else:
        tool_status.append(("Синтез речи (Edge TTS)", True, None))

    if subscription_features.modal.managed_by_nous:
        tool_status.append(("Исполнение в Modal (подписка Nous)", True, None))
    elif cfg_get(config, "terminal", "backend") == "modal":
        if subscription_features.modal.direct_override:
            tool_status.append(("Исполнение в Modal (прямой Modal)", True, None))
        else:
            tool_status.append(("Исполнение в Modal", False, "запустите 'hermes setup terminal'"))
    elif managed_nous_tools_enabled() and subscription_features.nous_auth_present:
        tool_status.append(("Исполнение в Modal (опционально через подписку Nous)", True, None))

    # Tinker + WandB (RL обучение)
    if get_env_value("TINKER_API_KEY") and get_env_value("WANDB_API_KEY"):
        tool_status.append(("RL обучение (Tinker)", True, None))
    elif get_env_value("TINKER_API_KEY"):
        tool_status.append(("RL обучение (Tinker)", False, "WANDB_API_KEY"))
    else:
        tool_status.append(("RL обучение (Tinker)", False, "TINKER_API_KEY"))

    # Home Assistant
    if get_env_value("HASS_TOKEN"):
        tool_status.append(("Умный дом (Home Assistant)", True, None))

    # Spotify (OAuth через hermes auth spotify — проверка auth.json, а не переменных окружения)
    try:
        from hermes_cli.auth import get_provider_auth_state
        _spotify_state = get_provider_auth_state("spotify") or {}
        if _spotify_state.get("access_token") or _spotify_state.get("refresh_token"):
            tool_status.append(("Spotify (PKCE OAuth)", True, None))
    except Exception:
        pass

    # Skills Hub
    if get_env_value("GITHUB_TOKEN"):
        tool_status.append(("Skills Hub (GitHub)", True, None))
    else:
        tool_status.append(("Skills Hub (GitHub)", False, "GITHUB_TOKEN"))

    # Терминал (всегда доступен, если выполнены системные зависимости)
    tool_status.append(("Терминал/Команды", True, None))

    # Планирование задач (всегда доступно, в памяти)
    tool_status.append(("Планирование задач (todo)", True, None))

    # Навыки (всегда доступны — встроенные + созданные пользователем)
    tool_status.append(("Навыки (просмотр, создание, редактирование)", True, None))

    # Вывод статуса
    available_count = sum(1 for _, avail, _ in tool_status if avail)
    total_count = len(tool_status)

    print_info(f"Доступно категорий инструментов: {available_count}/{total_count}")
    print()

    for name, available, missing_var in tool_status:
        if available:
            print(f"   {color('✓', Colors.GREEN)} {name}")
        else:
            print(
                f"   {color('✗', Colors.RED)} {name} {color(f'(отсутствует {missing_var})', Colors.DIM)}"
            )

    print()

    disabled_tools = [(name, var) for name, avail, var in tool_status if not avail]
    if disabled_tools:
        print_warning(
            "Некоторые инструменты отключены. Запустите 'hermes setup tools' для их настройки,",
        )
        from hermes_constants import display_hermes_home as _dhh
        print_warning(f"или отредактируйте {_dhh()}/.env напрямую, чтобы добавить недостающие API-ключи.")
        print()

    # Баннер завершения
    print()
    print(
        color(
            "┌─────────────────────────────────────────────────────────┐", Colors.GREEN
        )
    )
    print(
        color(
            "│              ✓ Настройка завершена!                      │", Colors.GREEN
        )
    )
    print(
        color(
            "└─────────────────────────────────────────────────────────┘", Colors.GREEN
        )
    )
    print()

    # Заметное отображение расположения файлов
    from hermes_constants import display_hermes_home as _dhh
    print(color(f"📁 Все ваши файлы находятся в {_dhh()}/:", Colors.CYAN, Colors.BOLD))
    print()
    print(f"   {color('Настройки:', Colors.YELLOW)}  {get_config_path()}")
    print(f"   {color('API-ключи:', Colors.YELLOW)}  {get_env_path()}")
    print(
        f"   {color('Данные:', Colors.YELLOW)}      {hermes_home}/cron/, sessions/, logs/"
    )
    print()

    print(color("─" * 60, Colors.DIM))
    print()
    print(color("📝 Для редактирования конфигурации:", Colors.CYAN, Colors.BOLD))
    print()
    print(f"   {color('hermes setup', Colors.GREEN)}          Запустить полный мастер настройки заново")
    print(f"   {color('hermes setup model', Colors.GREEN)}    Изменить модель/провайдера")
    print(f"   {color('hermes setup terminal', Colors.GREEN)} Изменить терминальный бэкенд")
    print(f"   {color('hermes setup gateway', Colors.GREEN)}  Настроить обмен сообщениями")
    print(f"   {color('hermes setup tools', Colors.GREEN)}    Настроить провайдеров инструментов")
    print()
    print(f"   {color('hermes config', Colors.GREEN)}         Просмотреть текущие настройки")
    print(
        f"   {color('hermes config edit', Colors.GREEN)}    Открыть конфиг в редакторе"
    )
    print(f"   {color('hermes config set <key> <value>', Colors.GREEN)}")
    print("                          Установить конкретное значение")
    print()
    print("   Или редактируйте файлы напрямую:")
    print(f"   {color(f'nano {get_config_path()}', Colors.DIM)}")
    print(f"   {color(f'nano {get_env_path()}', Colors.DIM)}")
    print()

    print(color("─" * 60, Colors.DIM))
    print()
    print(color("🚀 Все готово!", Colors.CYAN, Colors.BOLD))
    print()
    print(f"   {color('hermes', Colors.GREEN)}              Начать чат")
    print(f"   {color('hermes gateway', Colors.GREEN)}      Запустить шлюз обмена сообщениями")
    print(f"   {color('hermes doctor', Colors.GREEN)}       Проверить наличие проблем")
    print()


def _prompt_container_resources(config: dict):
    """Запрос настроек ресурсов контейнера (Docker, Singularity, Modal, Daytona)."""
    terminal = config.setdefault("terminal", {})

    print()
    print_info("Настройки ресурсов контейнера:")

    # Сохранение состояния
    current_persist = terminal.get("container_persistent", True)
    persist_label = "yes" if current_persist else "no"
    print_info("  Постоянная файловая система сохраняет файлы между сессиями.")
    print_info("  Установите 'no' для временных песочниц, которые сбрасываются каждый раз.")
    persist_str = prompt(
        "  Сохранять файловую систему между сессиями? (yes/no)", persist_label
    )
    terminal["container_persistent"] = persist_str.lower() in ("yes", "true", "y", "1")

    # CPU
    current_cpu = terminal.get("container_cpu", 1)
    cpu_str = prompt("  Ядра CPU", str(current_cpu))
    try:
        terminal["container_cpu"] = float(cpu_str)
    except ValueError:
        pass

    # Память
    current_mem = terminal.get("container_memory", 5120)
    mem_str = prompt("  Память в МБ (5120 = 5ГБ)", str(current_mem))
    try:
        terminal["container_memory"] = int(mem_str)
    except ValueError:
        pass

    # Диск
    current_disk = terminal.get("container_disk", 51200)
    disk_str = prompt("  Диск в МБ (51200 = 50ГБ)", str(current_disk))
    try:
        terminal["container_disk"] = int(disk_str)
    except ValueError:
        pass


def _prompt_vercel_sandbox_settings(config: dict):
    """Запрос настроек Vercel Sandbox без показа неподдерживаемых размеров диска."""
    terminal = config.setdefault("terminal", {})

    print()
    print_info("Настройки Vercel Sandbox:")
    print_info("  Сохранение файловой системы использует снимки (snapshots) Vercel.")
    print_info("  Снимки восстанавливают только файлы; активные процессы не продолжаются после воссоздания песочницы.")

    from tools.terminal_tool import _SUPPORTED_VERCEL_RUNTIMES

    current_runtime = terminal.get("vercel_runtime") or "node24"
    supported_label = ", ".join(_SUPPORTED_VERCEL_RUNTIMES)
    runtime = prompt(f"  Среда выполнения ({supported_label})", current_runtime).strip() or current_runtime
    if runtime not in _SUPPORTED_VERCEL_RUNTIMES:
        print_warning(f"Неподдерживаемая среда выполнения Vercel '{runtime}', оставляем {current_runtime}.")
        runtime = current_runtime if current_runtime in _SUPPORTED_VERCEL_RUNTIMES else "node24"
    terminal["vercel_runtime"] = runtime
    save_env_value("TERMINAL_VERCEL_RUNTIME", runtime)

    current_persist = terminal.get("container_persistent", True)
    persist_label = "yes" if current_persist else "no"
    terminal["container_persistent"] = prompt(
        "  Сохранять файловую систему с помощью снимков? (yes/no)", persist_label
    ).lower() in ("yes", "true", "y", "1")

    current_cpu = terminal.get("container_cpu", 1)
    cpu_str = prompt("  Ядра CPU", str(current_cpu))
    try:
        terminal["container_cpu"] = float(cpu_str)
    except ValueError:
        pass

    current_mem = terminal.get("container_memory", 5120)
    mem_str = prompt("  Память в МБ (5120 = 5ГБ)", str(current_mem))
    try:
        terminal["container_memory"] = int(mem_str)
    except ValueError:
        pass

    if terminal.get("container_disk", 51200) not in (0, 51200):
        print_warning("Vercel Sandbox не поддерживает пользовательские размеры диска; сброс container_disk на 51200.")
    terminal["container_disk"] = 51200

    print()
    print_info("Аутентификация Vercel:")
    print_info("  Используйте долгоживущий токен доступа Vercel вместе с ID проекта/команды.")
    linked_project = _read_nearest_vercel_project()
    if linked_project:
        print_info("  Найдены значения по умолчанию в ближайшем .vercel/project.json.")

    remove_env_value("VERCEL_OIDC_TOKEN")
    token = prompt("    Токен доступа Vercel", get_env_value("VERCEL_TOKEN") or "", password=True)
    project = prompt(
        "    ID проекта Vercel",
        get_env_value("VERCEL_PROJECT_ID") or linked_project.get("projectId", ""),
    )
    team = prompt(
        "    ID команды Vercel",
        get_env_value("VERCEL_TEAM_ID") or linked_project.get("orgId", ""),
    )
    if token:
        save_env_value("VERCEL_TOKEN", token)
    if project:
        save_env_value("VERCEL_PROJECT_ID", project)
    if team:
        save_env_value("VERCEL_TEAM_ID", team)


def _read_nearest_vercel_project(start: Path | None = None) -> dict[str, str]:
    """Чтение значений по умолчанию для проекта/команды из ближайшего файла связи Vercel."""
    current = (start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent

    for directory in (current, *current.parents):
        project_file = directory / ".vercel" / "project.json"
        if not project_file.exists():
            continue
        try:
            data = json.loads(project_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            key: value
            for key, value in {
                "projectId": data.get("projectId"),
                "orgId": data.get("orgId"),
            }.items()
            if isinstance(value, str) and value.strip()
        }
    return {}


# Категории инструментов и конфигурация провайдеров теперь находятся в tools_config.py
# (общий файл для `hermes tools` и `hermes setup tools`).


# =============================================================================
# Раздел 1: Конфигурация модели и провайдера
# =============================================================================



def setup_model_provider(config: dict, *, quick: bool = False):
    """Настройка провайдера логического вывода и модели по умолчанию.

    Делегирует выполнение ``cmd_model()`` (тот же поток, что и в ``hermes model``)
    для выбора провайдера, запроса учетных данных и выбора модели.
    Это обеспечивает единый путь выполнения для настройки всех провайдеров — любой новый
    провайдер, добавленный в ``hermes model``, автоматически становится доступен здесь.

    Если параметр *quick* равен True, пропускается ротация учетных данных, настройка
    Vision и TTS — используется для упрощенной первой быстрой настройки.
    """
    from hermes_cli.config import load_config, save_config

    print_header("Провайдер логического вывода")
    print_info("Выберите способ подключения к вашей основной чат-модели.")
    print_info(f"   Руководство: {_DOCS_BASE}/integrations/providers")
    print()

    # Делегирование общему потоку hermes model — обрабатывает выбор провайдера,
    # запрос учетных данных, выбор модели и сохранение конфигурации.
    from hermes_cli.main import select_provider_and_model
    try:
        select_provider_and_model()
    except (SystemExit, KeyboardInterrupt):
        print()
        print_info("Настройка провайдера пропущена.")
    except Exception as exc:
        logger.debug("Ошибка select_provider_and_model во время настройки: %s", exc)
        print_warning(f"При настройке провайдера возникла ошибка: {exc}")
        print_info("Вы можете попробовать еще раз позже с помощью: hermes model")

    # Синхронизация словаря конфигурации мастера с тем, что cmd_model сохранил на диск.
    # Это критически важно: cmd_model записывает на диск через свой собственный цикл загрузки/сохранения,
    # и финальный save_config(config) мастера не должен перезаписывать эти
    # изменения устаревшими значениями (#4172).
    _refreshed = load_config()
    config["model"] = _refreshed.get("model", config.get("model"))
    if "custom_providers" in _refreshed:
        config["custom_providers"] = _refreshed["custom_providers"]
    else:
        config.pop("custom_providers", None)

    # Определение выбранного провайдера для последующих шагов (настройка vision).
    selected_provider = None
    _m = config.get("model")
    if isinstance(_m, dict):
        selected_provider = _m.get("provider")

    # ── Настройка резервирования и ротации у того же провайдера (только полная настройка) ──
    if not quick and _supports_same_provider_pool_setup(selected_provider):
        try:
            from types import SimpleNamespace
            from agent.credential_pool import load_pool
            from hermes_cli.auth_commands import auth_add_command

            pool = load_pool(selected_provider)
            entries = pool.entries()
            entry_count = len(entries)
            manual_count = sum(1 for entry in entries if str(getattr(entry, "source", "")).startswith("manual"))
            auto_count = entry_count - manual_count
            print()
            print_header("Резервирование и ротация у того же провайдера")
            print_info(
                "Hermes может хранить несколько учетных данных для одного провайдера и переключаться между"
            )
            print_info(
                "ними, когда лимит исчерпан или сработало ограничение частоты запросов. Это сохраняет"
            )
            print_info(
                "вашего основного провайдера, уменьшая количество прерываний из-за проблем с квотами."
            )
            print()
            if auto_count > 0:
                print_info(
                    f"Текущие объединенные учетные данные для {selected_provider}: {entry_count} "
                    f"({manual_count} вручную, {auto_count} авто-обнаружено из env/общей аутентификации)"
                )
            else:
                print_info(f"Текущие объединенные учетные данные для {selected_provider}: {entry_count}")

            while prompt_yes_no("Добавить еще одни учетные данные для резервирования у того же провайдера?", False):
                auth_add_command(
                    SimpleNamespace(
                        provider=selected_provider,
                        auth_type="",
                        label=None,
                        api_key=None,
                        portal_url=None,
                        inference_url=None,
                        client_id=None,
                        scope=None,
                        no_browser=False,
                        timeout=15.0,
                        insecure=False,
                        ca_bundle=None,
                        min_key_ttl_seconds=5 * 60,
                    )
                )
                pool = load_pool(selected_provider)
                entry_count = len(pool.entries())
                print_info(f"Пул провайдера теперь содержит {entry_count} учетных данных.")

            if entry_count > 1:
                strategy_labels = [
                    "Сначала заполнение / привязка — использовать первые исправные учетные данные до исчерпания",
                    "Round robin — переключаться на следующие исправные учетные данные после каждого выбора",
                    "Случайно — каждый раз выбирать случайные исправные учетные данные",
                ]
                current_strategy = _get_credential_pool_strategies(config).get(selected_provider, "fill_first")
                default_strategy_idx = {
                    "fill_first": 0,
                    "round_robin": 1,
                    "random": 2,
                }.get(current_strategy, 0)
                strategy_idx = prompt_choice(
                    "Выберите стратегию ротации у того же провайдера:",
                    strategy_labels,
                    default_strategy_idx,
                )
                strategy_value = ["fill_first", "round_robin", "random"][strategy_idx]
                _set_credential_pool_strategy(config, selected_provider, strategy_value)
                print_success(f"Сохранена стратегия ротации для {selected_provider}: {strategy_value}")
        except Exception as exc:
            logger.debug("Не удалось настроить резервирование у того же провайдера: %s", exc)

    # ── Настройка Vision и анализа изображений (только полная настройка) ──
    if quick:
        _vision_needs_setup = False
    else:
        try:
            from agent.auxiliary_client import get_available_vision_backends
            _vision_backends = set(get_available_vision_backends())
        except Exception:
            _vision_backends = set()

        _vision_needs_setup = not bool(_vision_backends)

        if selected_provider in _vision_backends:
            _vision_needs_setup = False

    if _vision_needs_setup:
        _prov_names = {
            "nous-api": "API-ключ Nous Portal",
            "copilot": "GitHub Copilot",
            "copilot-acp": "GitHub Copilot ACP",
            "zai": "Z.AI / GLM",
            "kimi-coding": "Kimi / Moonshot",
            "kimi-coding-cn": "Kimi / Moonshot (Китай)",
            "stepfun": "StepFun Step Plan",
            "minimax": "MiniMax",
            "minimax-cn": "MiniMax CN",
            "anthropic": "Anthropic",
            "ai-gateway": "Vercel AI Gateway",
            "custom": "ваш пользовательский эндпоинт",
        }
        _prov_display = _prov_names.get(selected_provider, selected_provider or "ваш провайдер")

        print()
        print_header("Vision и анализ изображений (опционально)")
        print_info(f"Vision использует отдельный мультимодальный бэкенд. {_prov_display}")
        print_info("в настоящее время не предоставляет бэкенд, который Hermes может автоматически использовать для vision,")
        print_info("поэтому выберите бэкенд сейчас или пропустите и настройте позже.")
        print()

        _vision_choices = [
            "OpenRouter — использует Gemini (бесплатный уровень на openrouter.ai/keys)",
            "OpenAI-совместимый эндпоинт — базовый URL, API-ключ и модель vision",
            "Пропустить",
        ]
        _vision_idx = prompt_choice("Настройка vision:", _vision_choices, 2)

        if _vision_idx == 0:  # OpenRouter
            _or_key = prompt("  API-ключ OpenRouter", password=True).strip()
            if _or_key:
                save_env_value("OPENROUTER_API_KEY", _or_key)
                print_success("Ключ OpenRouter сохранен — vision будет использовать Gemini")
            else:
                print_info("Пропущено — vision будет недоступен")
        elif _vision_idx == 1:  # OpenAI-совместимый эндпоинт
            _base_url = prompt("  Базовый URL (пусто для OpenAI)").strip() or "https://api.openai.com/v1"
            _api_key_label = "  API-ключ"
            _is_native_openai = base_url_hostname(_base_url) == "api.openai.com"
            if _is_native_openai:
                _api_key_label = "  API-ключ OpenAI"
            _oai_key = prompt(_api_key_label, password=True).strip()
            if _oai_key:
                save_env_value("OPENAI_API_KEY", _oai_key)
                # Сохранение базового URL vision в конфиг (не в .env — там только секреты)
                _vaux = config.setdefault("auxiliary", {}).setdefault("vision", {})
                _vaux["base_url"] = _base_url
                if _is_native_openai:
                    _oai_vision_models = ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano"]
                    _vm_choices = _oai_vision_models + ["Использовать по умолчанию (gpt-4o-mini)"]
                    _vm_idx = prompt_choice("Выберите модель vision:", _vm_choices, 0)
                    _selected_vision_model = (
                        _oai_vision_models[_vm_idx]
                        if _vm_idx < len(_oai_vision_models)
                        else "gpt-4o-mini"
                    )
                else:
                    _selected_vision_model = prompt("  Модель vision (пусто = использовать основную/пользовательскую по умолчанию)").strip()
                if _selected_vision_model:
                    save_env_value("AUXILIARY_VISION_MODEL", _selected_vision_model)
                print_success(
                    f"Vision настроен с {_base_url}"
                    + (f" ({_selected_vision_model})" if _selected_vision_model else "")
                )
            else:
                print_info("Пропущено — vision будет недоступен")
        else:
            print_info("Пропущено — добавьте позже с помощью 'hermes setup' или настройте параметры AUXILIARY_VISION_*")


    # Подсказка Tool Gateway уже отображается функцией _model_flow_nous() выше.
    save_config(config)

    if not quick and selected_provider != "nous":
        _setup_tts_provider(config)


# =============================================================================
# Раздел 1b: Конфигурация TTS провайдера
# =============================================================================


def _check_espeak_ng() -> bool:
    """Проверка, установлен ли espeak-ng."""
    return shutil.which("espeak-ng") is not None or shutil.which("espeak") is not None


def _install_neutts_deps() -> bool:
    """Установка зависимостей NeuTTS с одобрения пользователя. Возвращает True в случае успеха."""
    import subprocess
    import sys

    # Проверка espeak-ng
    if not _check_espeak_ng():
        print()
        print_warning("Для NeuTTS требуется espeak-ng для фонемизации.")
        if sys.platform == "darwin":
            print_info("Установите с помощью: brew install espeak-ng")
        elif sys.platform == "win32":
            print_info("Установите с помощью: choco install espeak-ng")
        else:
            print_info("Установите с помощью: sudo apt install espeak-ng")
        print()
        if prompt_yes_no("Установить espeak-ng сейчас?", True):
            try:
                if sys.platform == "darwin":
                    subprocess.run(["brew", "install", "espeak-ng"], check=True)
                elif sys.platform == "win32":
                    subprocess.run(["choco", "install", "espeak-ng", "-y"], check=True)
                else:
                    subprocess.run(["sudo", "apt", "install", "-y", "espeak-ng"], check=True)
                print_success("espeak-ng установлен")
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                print_warning(f"Не удалось установить espeak-ng автоматически: {e}")
                print_info("Пожалуйста, установите его вручную и запустите настройку снова.")
                return False
        else:
            print_warning("Для NeuTTS требуется espeak-ng. Установите его вручную перед использованием NeuTTS.")

    # Установка Python-пакета neutts
    print()
    print_info("Установка Python-пакета neutts...")
    print_info("Это также приведет к загрузке модели TTS (~300 МБ) при первом использовании.")
    print()
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "neutts[all]", "--quiet"],
            check=True, timeout=300,
        )
        print_success("neutts успешно установлен")
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print_error(f"Не удалось установить neutts: {e}")
        print_info("Попробуйте вручную: python -m pip install -U neutts[all]")
        return False


def _install_kittentts_deps() -> bool:
    """Установка зависимостей KittenTTS с одобрения пользователя. Возвращает True в случае успеха."""
    import subprocess
    import sys

    wheel_url = (
        "https://github.com/KittenML/KittenTTS/releases/download/"
        "0.8.1/kittentts-0.8.1-py3-none-any.whl"
    )
    print()
    print_info("Установка Python-пакета kittentts (модель ~25-80 МБ будет загружена при первом использовании)...")
    print()
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", wheel_url, "soundfile", "--quiet"],
            check=True, timeout=300,
        )
        print_success("kittentts успешно установлен")
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print_error(f"Не удалось установить kittentts: {e}")
        print_info(f"Попробуйте вручную: python -m pip install -U '{wheel_url}' soundfile")
        return False


def _setup_tts_provider(config: dict):
    """Интерактивный выбор провайдера TTS с процессом установки для NeuTTS."""
    tts_config = config.get("tts", {})
    current_provider = tts_config.get("provider", "edge")
    subscription_features = get_nous_subscription_features(config)

    provider_labels = {
        "edge": "Edge TTS",
        "elevenlabs": "ElevenLabs",
        "openai": "OpenAI TTS",
        "xai": "xAI TTS",
        "minimax": "MiniMax TTS",
        "mistral": "Mistral Voxtral TTS",
        "gemini": "Google Gemini TTS",
        "neutts": "NeuTTS",
        "kittentts": "KittenTTS",
    }
    current_label = provider_labels.get(current_provider, current_provider)

    print()
    print_header("Провайдер синтеза речи (TTS, опционально)")
    print_info(f"Текущий: {current_label}")
    print()

    choices = []
    providers = []
    if managed_nous_tools_enabled() and subscription_features.nous_auth_present:
        choices.append("Подписка Nous (управляемый OpenAI TTS, оплата через вашу подписку)")
        providers.append("nous-openai")
    choices.extend(
        [
            "Edge TTS (бесплатно, облачный, настройка не требуется)",
            "ElevenLabs (премиум-качество, требуется API-ключ)",
            "OpenAI TTS (хорошее качество, требуется API-ключ)",
            "xAI TTS (голоса Grok, требуется API-ключ)",
            "MiniMax TTS (высокое качество с клонированием голоса, требуется API-ключ)",
            "Mistral Voxtral TTS (многоязычный, нативный Opus, требуется API-ключ)",
            "Google Gemini TTS (30 встроенных голосов, управление промптом, требуется API-ключ)",
            "NeuTTS (локально на устройстве, бесплатно, загрузка модели ~300 МБ)",
            "KittenTTS (локально на устройстве, бесплатно, легкий ~25-80 МБ ONNX)",
        ]
    )
    providers.extend(["edge", "elevenlabs", "openai", "xai", "minimax", "mistral", "gemini", "neutts", "kittentts"])
    choices.append(f"Оставить текущий ({current_label})")
    keep_current_idx = len(choices) - 1
    idx = prompt_choice("Выберите провайдера TTS:", choices, keep_current_idx)

    if idx == keep_current_idx:
        return

    selected = providers[idx]
    selected_via_nous = selected == "nous-openai"
    if selected == "nous-openai":
        selected = "openai"
        print_info("OpenAI TTS будет использовать управляемый шлюз Nous и оплачиваться через вашу подписку.")
        if get_env_value("VOICE_TOOLS_OPENAI_KEY") or get_env_value("OPENAI_API_KEY"):
            print_warning(
                "Прямые учетные данные OpenAI все еще настроены и могут иметь приоритет, пока не будут удалены из ~/.hermes/.env."
            )

    if selected == "neutts":
        # Проверка, установлен ли уже
        try:
            already_installed = importlib.util.find_spec("neutts") is not None
        except Exception:
            already_installed = False

        if already_installed:
            print_success("NeuTTS уже установлен")
        else:
            print()
            print_info("Для NeuTTS требуется:")
            print_info("  • Python-пакет: neutts (~50 МБ установка + ~300 МБ модель при первом использовании)")
            print_info("  • Системный пакет: espeak-ng (фонемизатор)")
            print()
            if prompt_yes_no("Установить зависимости NeuTTS сейчас?", True):
                if not _install_neutts_deps():
                    print_warning("Установка NeuTTS не завершена. Возврат к Edge TTS.")
                    selected = "edge"
            else:
                print_info("Установка пропущена. Установите tts.provider в 'neutts' после ручной установки.")
                selected = "edge"

    elif selected == "elevenlabs":
        existing = get_env_value("ELEVENLABS_API_KEY")
        if not existing:
            print()
            api_key = prompt("API-ключ ElevenLabs", password=True)
            if api_key:
                save_env_value("ELEVENLABS_API_KEY", api_key)
                print_success("API-ключ ElevenLabs сохранен")
            else:
                print_warning("API-ключ не предоставлен. Возврат к Edge TTS.")
                selected = "edge"

    elif selected == "openai" and not selected_via_nous:
        existing = get_env_value("VOICE_TOOLS_OPENAI_KEY") or get_env_value("OPENAI_API_KEY")
        if not existing:
            print()
            api_key = prompt("API-ключ OpenAI для TTS", password=True)
            if api_key:
                save_env_value("VOICE_TOOLS_OPENAI_KEY", api_key)
                print_success("API-ключ OpenAI TTS сохранен")
            else:
                print_warning("API-ключ не предоставлен. Возврат к Edge TTS.")
                selected = "edge"

    elif selected == "xai":
        existing = get_env_value("XAI_API_KEY")
        if not existing:
            print()
            api_key = prompt("API-ключ xAI для TTS", password=True)
            if api_key:
                save_env_value("XAI_API_KEY", api_key)
                print_success("API-ключ xAI TTS сохранен")
            else:
                from hermes_constants import display_hermes_home as _dhh
                print_warning(
                    "API-ключ xAI для TTS не предоставлен. Настройте XAI_API_KEY с помощью "
                    f"hermes setup model или в {_dhh()}/.env для использования xAI TTS. "
                    "Возврат к Edge TTS."
                )
                selected = "edge"
        if selected == "xai":
            print()
            voice_id = prompt("xAI voice_id (Enter для 'eve' или вставьте ID пользовательского голоса)")
            if voice_id and voice_id.strip():
                config.setdefault("tts", {}).setdefault("xai", {})["voice_id"] = voice_id.strip()
                print_success(f"xAI voice_id установлен: {voice_id.strip()}")


    elif selected == "minimax":
        existing = get_env_value("MINIMAX_API_KEY")
        if not existing:
            print()
            api_key = prompt("API-ключ MiniMax для TTS", password=True)
            if api_key:
                save_env_value("MINIMAX_API_KEY", api_key)
                print_success("API-ключ MiniMax TTS сохранен")
            else:
                print_warning("API-ключ не предоставлен. Возврат к Edge TTS.")
                selected = "edge"

    elif selected == "mistral":
        existing = get_env_value("MISTRAL_API_KEY")
        if not existing:
            print()
            api_key = prompt("API-ключ Mistral для TTS", password=True)
            if api_key:
                save_env_value("MISTRAL_API_KEY", api_key)
                print_success("API-ключ Mistral TTS сохранен")
            else:
                print_warning("API-ключ не предоставлен. Возврат к Edge TTS.")
                selected = "edge"

    elif selected == "gemini":
        existing = get_env_value("GEMINI_API_KEY") or get_env_value("GOOGLE_API_KEY")
        if not existing:
            print()
            print_info("Получите бесплатный API-ключ на https://aistudio.google.com/app/apikey")
            api_key = prompt("API-ключ Gemini для TTS", password=True)
            if api_key:
                save_env_value("GEMINI_API_KEY", api_key)
                print_success("API-ключ Gemini TTS сохранен")
            else:
                print_warning("API-ключ не предоставлен. Возврат к Edge TTS.")
                selected = "edge"

    elif selected == "kittentts":
        # Проверка, установлен ли уже
        try:
            import importlib.util
            already_installed = importlib.util.find_spec("kittentts") is not None
        except Exception:
            already_installed = False

        if already_installed:
            print_success("KittenTTS уже установлен")
        else:
            print()
            print_info("KittenTTS — легкий (~25-80 МБ, только CPU, API-ключ не требуется).")
            print_info("Голоса: Jasper, Bella, Luna, Bruno, Rosie, Hugo, Kiki, Leo")
            print()
            if prompt_yes_no("Установить KittenTTS сейчас?", True):
                if not _install_kittentts_deps():
                    print_warning("Установка KittenTTS не завершена. Возврат к Edge TTS.")
                    selected = "edge"
            else:
                print_info("Установка пропущена. Установите tts.provider в 'kittentts' после ручной установки.")
                selected = "edge"

    # Сохранение выбора
    if "tts" not in config:
        config["tts"] = {}
    config["tts"]["provider"] = selected
    save_config(config)
    print_success(f"Провайдер TTS установлен: {provider_labels.get(selected, selected)}")


def setup_tts(config: dict):
    """Отдельная настройка TTS (для 'hermes setup tts')."""
    _setup_tts_provider(config)


# =============================================================================
# Раздел 2: Конфигурация терминального бэкенда
# =============================================================================


def setup_terminal_backend(config: dict):
    """Настройка бэкенда выполнения в терминале."""
    import platform as _platform
    print_header("Терминальный бэкенд")
    print_info("Выберите, где Hermes будет запускать shell-команды и код.")
    print_info("Это влияет на выполнение инструментов, доступ к файлам и изоляцию.")
    print_info(f"   Руководство: {_DOCS_BASE}/developer-guide/environments")
    print()

    current_backend = cfg_get(config, "terminal", "backend", default="local")
    is_linux = _platform.system() == "Linux"

    # Создание списка бэкендов с описаниями
    terminal_choices = [
        "Local - запуск прямо на этой машине (по умолчанию)",
        "Docker - изолированный контейнер с настраиваемыми ресурсами",
        "Modal - бессерверная облачная песочница",
        "SSH - запуск на удаленной машине",
        "Daytona - постоянная облачная среда разработки",
        "Vercel Sandbox - облачная микро-VM с сохранением файловой системы через снимки",
    ]
    idx_to_backend = {0: "local", 1: "docker", 2: "modal", 3: "ssh", 4: "daytona", 5: "vercel_sandbox"}
    backend_to_idx = {"local": 0, "docker": 1, "modal": 2, "ssh": 3, "daytona": 4, "vercel_sandbox": 5}

    next_idx = 6
    if is_linux:
        terminal_choices.append("Singularity/Apptainer - контейнер, дружелюбный к HPC")
        idx_to_backend[next_idx] = "singularity"
        backend_to_idx["singularity"] = next_idx
        next_idx += 1

    # Добавление опции сохранения текущего
    keep_current_idx = next_idx
    terminal_choices.append(f"Оставить текущий ({current_backend})")
    idx_to_backend[keep_current_idx] = current_backend

    terminal_idx = prompt_choice(
        "Выберите терминальный бэкенд:", terminal_choices, keep_current_idx
    )

    selected_backend = idx_to_backend.get(terminal_idx)

    if terminal_idx == keep_current_idx:
        print_info(f"Оставляем текущий бэкенд: {current_backend}")
        return

    config.setdefault("terminal", {})["backend"] = selected_backend

    if selected_backend == "local":
        print_success("Терминальный бэкенд: Local")
        print_info("Команды запускаются прямо на этой машине.")

        # Рабочая директория шлюза/cron
        print()
        print_info("Рабочая директория шлюза:")
        print_info("  Используется сессиями Telegram/Discord/cron.")
        print_info("  CLI/TUI всегда использует вашу текущую директорию запуска.")
        current_cwd = cfg_get(config, "terminal", "cwd", default="")
        cwd = prompt("  Рабочая директория шлюза", current_cwd or str(Path.home()))
        if cwd:
            config["terminal"]["cwd"] = cwd

        # Поддержка sudo
        print()
        existing_sudo = get_env_value("SUDO_PASSWORD")
        if existing_sudo:
            print_info("Пароль sudo: настроен")
        else:
            if prompt_yes_no(
                "Включить поддержку sudo? (сохраняет пароль для apt install и т.д.)", False
            ):
                sudo_pass = prompt("  Пароль sudo", password=True)
                if sudo_pass:
                    save_env_value("SUDO_PASSWORD", sudo_pass)
                    print_success("Пароль sudo сохранен")

    elif selected_backend == "docker":
        print_success("Терминальный бэкенд: Docker")

        # Проверка доступности Docker
        docker_bin = shutil.which("docker")
        if not docker_bin:
            print_warning("Docker не найден в PATH!")
            print_info("Установите Docker: https://docs.docker.com/get-docker/")
        else:
            print_info(f"Docker найден: {docker_bin}")

        # Docker-образ
        current_image = cfg_get(config, "terminal", "docker_image", default="nikolaik/python-nodejs:python3.11-nodejs20")
        image = prompt("  Docker-образ", current_image)
        config["terminal"]["docker_image"] = image
        save_env_value("TERMINAL_DOCKER_IMAGE", image)

        _prompt_container_resources(config)

    elif selected_backend == "singularity":
        print_success("Терминальный бэкенд: Singularity/Apptainer")

        # Проверка доступности singularity/apptainer
        sing_bin = shutil.which("apptainer") or shutil.which("singularity")
        if not sing_bin:
            print_warning("Singularity/Apptainer не найден в PATH!")
            print_info(
                "Установка: https://apptainer.org/docs/admin/main/installation.html"
            )
        else:
            print_info(f"Найдено: {sing_bin}")

        current_image = cfg_get(config, "terminal", "singularity_image", default="docker://nikolaik/python-nodejs:python3.11-nodejs20")
        image = prompt("  Образ контейнера", current_image)
        config["terminal"]["singularity_image"] = image
        save_env_value("TERMINAL_SINGULARITY_IMAGE", image)

        _prompt_container_resources(config)

    elif selected_backend == "modal":
        print_success("Терминальный бэкенд: Modal")
        print_info("Бессерверные облачные песочницы. Каждая сессия получает свой контейнер.")
        from tools.managed_tool_gateway import is_managed_tool_gateway_ready
        from tools.tool_backend_helpers import normalize_modal_mode

        managed_modal_available = bool(
            managed_nous_tools_enabled()
            and
            get_nous_subscription_features(config).nous_auth_present
            and is_managed_tool_gateway_ready("modal")
        )
        modal_mode = normalize_modal_mode(cfg_get(config, "terminal", "modal_mode"))
        use_managed_modal = False
        if managed_modal_available:
            modal_choices = [
                "Использовать мою подписку Nous",
                "Использовать мой собственный аккаунт Modal",
            ]
            if modal_mode == "managed":
                default_modal_idx = 0
            elif modal_mode == "direct":
                default_modal_idx = 1
            else:
                default_modal_idx = 1 if get_env_value("MODAL_TOKEN_ID") else 0
            modal_mode_idx = prompt_choice(
                "Выберите способ оплаты выполнения в Modal:",
                modal_choices,
                default_modal_idx,
            )
            use_managed_modal = modal_mode_idx == 0

        if use_managed_modal:
            config["terminal"]["modal_mode"] = "managed"
            print_info("Выполнение в Modal будет использовать управляемый шлюз Nous и оплачиваться через вашу подписку.")
            if get_env_value("MODAL_TOKEN_ID") or get_env_value("MODAL_TOKEN_SECRET"):
                print_info(
                    "Прямые учетные данные Modal все еще настроены, но этот бэкенд закреплен за управляемым режимом."
                )
        else:
            config["terminal"]["modal_mode"] = "direct"
            print_info("Требуется аккаунт Modal: https://modal.com")

            # Проверка, установлен ли modal SDK
            try:
                __import__("modal")
            except ImportError:
                print_info("Установка modal SDK...")
                import subprocess

                uv_bin = shutil.which("uv")
                if uv_bin:
                    result = subprocess.run(
                        [
                            uv_bin,
                            "pip",
                            "install",
                            "--python",
                            sys.executable,
                            "modal",
                        ],
                        capture_output=True,
                        text=True,
                    )
                else:
                    result = subprocess.run(
                        [sys.executable, "-m", "pip", "install", "modal"],
                        capture_output=True,
                        text=True,
                    )
                if result.returncode == 0:
                    print_success("modal SDK установлен")
                else:
                    print_warning("Установка не удалась — запустите вручную: pip install modal")

            # Токен Modal
            print()
            print_info("Аутентификация Modal:")
            print_info("  Получите ваш токен на: https://modal.com/settings")
            existing_token = get_env_value("MODAL_TOKEN_ID")
            if existing_token:
                print_info("  Токен Modal: уже настроен")
                if prompt_yes_no("  Обновить учетные данные Modal?", False):
                    token_id = prompt("    Modal Token ID", password=True)
                    token_secret = prompt("    Modal Token Secret", password=True)
                    if token_id:
                        save_env_value("MODAL_TOKEN_ID", token_id)
                    if token_secret:
                        save_env_value("MODAL_TOKEN_SECRET", token_secret)
            else:
                token_id = prompt("    Modal Token ID", password=True)
                token_secret = prompt("    Modal Token Secret", password=True)
                if token_id:
                    save_env_value("MODAL_TOKEN_ID", token_id)
                if token_secret:
                    save_env_value("MODAL_TOKEN_SECRET", token_secret)

        _prompt_container_resources(config)

    elif selected_backend == "daytona":
        print_success("Терминальный бэкенд: Daytona")
        print_info("Постоянные облачные среды разработки.")
        print_info("Каждая сессия получает выделенную песочницу с сохранением файловой системы.")
        print_info("Зарегистрируйтесь на: https://daytona.io")

        # Проверка, установлен ли daytona SDK
        try:
            __import__("daytona")
        except ImportError:
            print_info("Установка daytona SDK...")
            import subprocess

            uv_bin = shutil.which("uv")
            if uv_bin:
                result = subprocess.run(
                    [uv_bin, "pip", "install", "--python", sys.executable, "daytona"],
                    capture_output=True,
                    text=True,
                )
            else:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "daytona"],
                    capture_output=True,
                    text=True,
                )
            if result.returncode == 0:
                print_success("daytona SDK установлен")
            else:
                print_warning("Установка не удалась — запустите вручную: pip install daytona")
                if result.stderr:
                    print_info(f"  Ошибка: {result.stderr.strip().splitlines()[-1]}")

        # API-ключ Daytona
        print()
        existing_key = get_env_value("DAYTONA_API_KEY")
        if existing_key:
            print_info("  API-ключ Daytona: уже настроен")
            if prompt_yes_no("  Обновить API-ключ?", False):
                api_key = prompt("    API-ключ Daytona", password=True)
                if api_key:
                    save_env_value("DAYTONA_API_KEY", api_key)
                    print_success("    Обновлено")
        else:
            api_key = prompt("    API-ключ Daytona", password=True)
            if api_key:
                save_env_value("DAYTONA_API_KEY", api_key)
                print_success("    Настроено")

        # Образ Daytona
        current_image = cfg_get(config, "terminal", "daytona_image", default="nikolaik/python-nodejs:python3.11-nodejs20")
        image = prompt("  Образ песочницы", current_image)
        config["terminal"]["daytona_image"] = image
        save_env_value("TERMINAL_DAYTONA_IMAGE", image)

        _prompt_container_resources(config)

    elif selected_backend == "vercel_sandbox":
        print_success("Терминальный бэкенд: Vercel Sandbox")
        print_info("Облачные песочницы microVM с сохранением файловой системы через снимки.")
        print_info("Требуется дополнительный SDK: pip install 'hermes-agent[vercel]'")

        try:
            __import__("vercel")
        except ImportError:
            print_info("Установка vercel SDK...")
            import subprocess

            uv_bin = shutil.which("uv")
            if uv_bin:
                result = subprocess.run(
                    [uv_bin, "pip", "install", "--python", sys.executable, "vercel"],
                    capture_output=True,
                    text=True,
                )
            else:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "vercel"],
                    capture_output=True,
                    text=True,
                )
            if result.returncode == 0:
                print_success("vercel SDK установлен")
            else:
                print_warning("Установка не удалась — запустите вручную: pip install 'hermes-agent[vercel]'")
                if result.stderr:
                    print_info(f"  Ошибка: {result.stderr.strip().splitlines()[-1]}")

        _prompt_vercel_sandbox_settings(config)

    elif selected_backend == "ssh":
        print_success("Терминальный бэкенд: SSH")
        print_info("Запуск команд на удаленной машине через SSH.")

        # SSH host
        current_host = get_env_value("TERMINAL_SSH_HOST") or ""
        host = prompt("  SSH хост (имя или IP)", current_host)
        if host:
            save_env_value("TERMINAL_SSH_HOST", host)

        # SSH пользователь
        current_user = get_env_value("TERMINAL_SSH_USER") or ""
        user = prompt("  SSH пользователь", current_user or os.getenv("USER", ""))
        if user:
            save_env_value("TERMINAL_SSH_USER", user)

        # SSH порт
        current_port = get_env_value("TERMINAL_SSH_PORT") or "22"
        port = prompt("  SSH порт", current_port)
        if port and port != "22":
            save_env_value("TERMINAL_SSH_PORT", port)

        # SSH ключ
        current_key = get_env_value("TERMINAL_SSH_KEY") or ""
        default_key = str(Path.home() / ".ssh" / "id_rsa")
        ssh_key = prompt("  Путь к приватному ключу SSH", current_key or default_key)
        if ssh_key:
            save_env_value("TERMINAL_SSH_KEY", ssh_key)

        # Проверка соединения
        if host and prompt_yes_no("  Проверить SSH-соединение?", True):
            print_info("  Проверка соединения...")
            import subprocess

            ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
            if ssh_key:
                ssh_cmd.extend(["-i", ssh_key])
            if port and port != "22":
                ssh_cmd.extend(["-p", port])
            ssh_cmd.append(f"{user}@{host}" if user else host)
            ssh_cmd.append("echo ok")
            result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                print_success("  SSH-соединение успешно!")
            else:
                print_warning(f"  Ошибка SSH-соединения: {result.stderr.strip()}")
                print_info("  Проверьте ваш SSH-ключ и настройки хоста.")

    # Синхронизация терминального бэкенда в .env, чтобы terminal_tool подхватил его напрямую.
    # config.yaml является первоисточником, но terminal_tool читает TERMINAL_ENV.
    save_env_value("TERMINAL_ENV", selected_backend)
    if selected_backend == "modal":
        save_env_value("TERMINAL_MODAL_MODE", config["terminal"].get("modal_mode", "auto"))
    if selected_backend == "vercel_sandbox":
        save_env_value("TERMINAL_VERCEL_RUNTIME", config["terminal"].get("vercel_runtime", "node24"))
    save_config(config)
    print()
    print_success(f"Терминальный бэкенд установлен: {selected_backend}")


# =============================================================================
# Раздел 3: Настройки агента
# =============================================================================


def _apply_default_agent_settings(config: dict):
    """Применение рекомендуемых настроек агента по умолчанию без запроса."""
    config.setdefault("agent", {})["max_turns"] = 90
    # config.yaml — это авторитетный источник для max_turns; шлюз
    # переносит его в HERMES_MAX_ITERATIONS при запуске. Мы больше не записываем
    # это в .env, чтобы избежать несогласованности двух источников.
    remove_env_value("HERMES_MAX_ITERATIONS")

    config.setdefault("display", {})["tool_progress"] = "all"

    config.setdefault("compression", {})["enabled"] = True
    config["compression"]["threshold"] = 0.50

    config.setdefault("session_reset", {}).update({
        "mode": "both",
        "idle_minutes": 1440,
        "at_hour": 4,
    })

    save_config(config)
    print_success("Применены рекомендуемые настройки по умолчанию:")
    print_info("  Макс. итераций: 90")
    print_info("  Прогресс инструментов: all")
    print_info("  Порог сжатия: 0.50")
    print_info("  Сброс сессии: неактивность (1440 мин) + ежедневно (4:00)")
    print_info("  Запустите `hermes setup agent` позже для настройки.")


def setup_agent_settings(config: dict):
    """Настройка поведения агента: итерации, отображение прогресса, сжатие, сброс сессии."""

    print_header("Настройки агента")
    print_info(f"   Руководство: {_DOCS_BASE}/user-guide/configuration")
    print()

    # ── Максимальное количество итераций ──
    # config.yaml является авторитетным источником; читаем оттуда. Если осталась
    # устаревшая запись в .env, предпочитаем значение из конфига.
    current_max = str(cfg_get(config, "agent", "max_turns", default=90))
    print_info("Максимальное количество итераций вызова инструментов за один разговор.")
    print_info("Выше = более сложные задачи, но больше расход токенов.")
    print_info(
        f"Нажмите Enter, чтобы оставить {current_max}. Используйте 90 для большинства задач или 150+ для свободного исследования."
    )

    max_iter_str = prompt("Макс. итераций", current_max)
    try:
        max_iter = int(max_iter_str)
        if max_iter > 0:
            # Пишем только в config.yaml (авторитетный источник). Также удаляем
            # устаревшую запись из .env.
            config.setdefault("agent", {})["max_turns"] = max_iter
            config.pop("max_turns", None)
            remove_env_value("HERMES_MAX_ITERATIONS")
            print_success(f"Макс. итераций установлено на {max_iter}")
    except ValueError:
        print_warning("Некорректное число, сохраняем текущее значение")

    # ── Отображение прогресса инструментов ──
    print_info("")
    print_info("Отображение прогресса инструментов")
    print_info("Управляет тем, насколько подробно показывается работа инструментов (в CLI и мессенджерах).")
    print_info("  off     — Тихий режим, только финальный ответ")
    print_info("  new     — Показывать имя инструмента только при его изменении (меньше шума)")
    print_info("  all     — Показывать каждый вызов инструмента с коротким предпросмотром")
    print_info("  verbose — Полные аргументы, результаты и отладочные логи")

    current_mode = cfg_get(config, "display", "tool_progress", default="all")
    mode = prompt("Режим прогресса инструментов", current_mode)
    if mode.lower() in ("off", "new", "all", "verbose"):
        if "display" not in config:
            config["display"] = {}
        config["display"]["tool_progress"] = mode.lower()
        save_config(config)
        print_success(f"Режим прогресса инструментов установлен на: {mode.lower()}")
    else:
        print_warning(f"Неизвестный режим '{mode}', оставляем '{current_mode}'")

    # ── Сжатие контекста ──
    print_header("Сжатие контекста")
    print_info("Автоматически резюмирует старые сообщения, когда контекст становится слишком длинным.")
    print_info(
        "Выше порог = сжимать позже (использовать больше контекста). Ниже = сжимать раньше."
    )

    config.setdefault("compression", {})["enabled"] = True

    current_threshold = cfg_get(config, "compression", "threshold", default=0.50)
    threshold_str = prompt("Порог сжатия (0.5-0.95)", str(current_threshold))
    try:
        threshold = float(threshold_str)
        if 0.5 <= threshold <= 0.95:
            config["compression"]["threshold"] = threshold
    except ValueError:
        pass

    print_success(
        f"Порог сжатия контекста установлен на {config['compression'].get('threshold', 0.50)}"
    )

    # ── Политика сброса сессии ──
    print_header("Политика сброса сессии")
    print_info(
        "Сессии в мессенджерах (Telegram, Discord и др.) со временем накапливают контекст."
    )
    print_info(
        "Каждое сообщение добавляется к истории разговора, что увеличивает затраты на API."
    )
    print_info("")
    print_info(
        "Чтобы управлять этим, сессии могут автоматически сбрасываться после периода неактивности"
    )
    print_info(
        "или в фиксированное время каждый день. При сбросе агент сначала сохраняет важные"
    )
    print_info(
        "данные в свою постоянную память, после чего контекст разговора очищается."
    )
    print_info("")
    print_info("Вы также можете вручную сбросить сессию в любое время, набрав /reset в чате.")
    print_info("")

    reset_choices = [
        "Неактивность + ежедневный сброс (рекомендуется — сброс того, что наступит раньше)",
        "Только неактивность (сброс через N минут отсутствия сообщений)",
        "Только ежедневно (сброс в фиксированный час каждый день)",
        "Никогда не сбрасывать автоматически (контекст живет до /reset или сжатия)",
        "Оставить текущие настройки",
    ]

    current_policy = config.get("session_reset", {})
    current_mode = current_policy.get("mode", "both")
    current_idle = current_policy.get("idle_minutes", 1440)
    current_hour = current_policy.get("at_hour", 4)

    default_reset = {"both": 0, "idle": 1, "daily": 2, "none": 3}.get(current_mode, 0)

    reset_idx = prompt_choice("Режим сброса сессии:", reset_choices, default_reset)

    config.setdefault("session_reset", {})

    if reset_idx == 0:  # Оба
        config["session_reset"]["mode"] = "both"
        idle_str = prompt("  Тайм-аут неактивности (минуты)", str(current_idle))
        try:
            idle_val = int(idle_str)
            if idle_val > 0:
                config["session_reset"]["idle_minutes"] = idle_val
        except ValueError:
            pass
        hour_str = prompt("  Час ежедневного сброса (0-23, местное время)", str(current_hour))
        try:
            hour_val = int(hour_str)
            if 0 <= hour_val <= 23:
                config["session_reset"]["at_hour"] = hour_val
        except ValueError:
            pass
        print_success(
            f"Сессии сбрасываются после {config['session_reset'].get('idle_minutes', 1440)} мин неактивности или ежедневно в {config['session_reset'].get('at_hour', 4)}:00"
        )
    elif reset_idx == 1:  # Только неактивность
        config["session_reset"]["mode"] = "idle"
        idle_str = prompt("  Тайм-аут неактивности (минуты)", str(current_idle))
        try:
            idle_val = int(idle_str)
            if idle_val > 0:
                config["session_reset"]["idle_minutes"] = idle_val
        except ValueError:
            pass
        print_success(
            f"Сессии сбрасываются после {config['session_reset'].get('idle_minutes', 1440)} мин неактивности"
        )
    elif reset_idx == 2:  # Только ежедневно
        config["session_reset"]["mode"] = "daily"
        hour_str = prompt("  Час ежедневного сброса (0-23, местное время)", str(current_hour))
        try:
            hour_val = int(hour_str)
            if 0 <= hour_val <= 23:
                config["session_reset"]["at_hour"] = hour_val
        except ValueError:
            pass
        print_success(
            f"Сессии сбрасываются ежедневно в {config['session_reset'].get('at_hour', 4)}:00"
        )
    elif reset_idx == 3:  # Никак
        config["session_reset"]["mode"] = "none"
        print_info(
            "Сессии никогда не будут сбрасываться автоматически. Контекст управляется только сжатием."
        )
        print_warning(
            "Стоимость долгих разговоров будет расти. Используйте /reset вручную при необходимости."
        )
    # else: оставить текущие (idx == 4)

    save_config(config)


# =============================================================================
# Раздел 4: Платформы обмена сообщениями (Шлюз)
# =============================================================================


def _setup_telegram():
    """Настройка учетных данных бота Telegram и списка разрешенных пользователей."""
    print_header("Telegram")
    existing = get_env_value("TELEGRAM_BOT_TOKEN")
    if existing:
        print_info("Telegram: уже настроен")
        if not prompt_yes_no("Перенастроить Telegram?", False):
            # Проверка отсутствующего списка разрешений в существующем конфиге
            if not get_env_value("TELEGRAM_ALLOWED_USERS"):
                print_info("⚠️ В Telegram не настроен список разрешенных пользователей — любой может использовать вашего бота!")
                if prompt_yes_no("Добавить разрешенных пользователей сейчас?", True):
                    print_info("   Чтобы узнать свой Telegram ID: напишите боту @userinfobot")
                    allowed_users = prompt("ID разрешенных пользователей (через запятую)")
                    if allowed_users:
                        save_env_value("TELEGRAM_ALLOWED_USERS", allowed_users.replace(" ", ""))
                        print_success("Список разрешенных пользователей Telegram настроен")
            return

    print_info("Создайте бота через @BotFather в Telegram")
    import re

    while True:
        token = prompt("Токен бота Telegram", password=True)
        if not token:
            return
        if not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", token):
            print_error(
                "Неверный формат токена. Ожидалось: <numeric_id>:<alphanumeric_hash> "
                "(например, 123456789:ABCdefGHI-jklMNOpqrSTUvwxYZ)"
            )
            continue
        break
    save_env_value("TELEGRAM_BOT_TOKEN", token)
    print_success("Токен Telegram сохранен")

    print()
    print_info("🔒 Безопасность: Ограничьте тех, кто может использовать вашего бота")
    print_info("   Чтобы узнать свой Telegram ID:")
    print_info("   1. Напишите боту @userinfobot в Telegram")
    print_info("   2. Он ответит вашим числовым ID (например, 123456789)")
    print()
    allowed_users = prompt(
        "ID разрешенных пользователей (через запятую, оставьте пустым для открытого доступа)"
    )
    if allowed_users:
        save_env_value("TELEGRAM_ALLOWED_USERS", allowed_users.replace(" ", ""))
        print_success("Список разрешенных пользователей Telegram настроен — только указанные пользователи могут использовать бота")
    else:
        print_info("⚠️ Список разрешенных пользователей не установлен — любой, кто найдет вашего бота, сможет им пользоваться!")

    print()
    print_info("📬 Домашний канал: куда Hermes доставляет результаты заданий cron,")
    print_info("   межплатформенные сообщения и уведомления.")
    print_info("   Для личных сообщений в Telegram это ваш ID пользователя (тот же, что выше).")

    first_user_id = allowed_users.split(",")[0].strip() if allowed_users else ""
    if first_user_id:
        if prompt_yes_no(f"Использовать ваш ID пользователя ({first_user_id}) в качестве домашнего канала?", True):
            save_env_value("TELEGRAM_HOME_CHANNEL", first_user_id)
            print_success(f"Домашний канал Telegram установлен на {first_user_id}")
        else:
            home_channel = prompt("ID домашнего канала (или оставьте пустым, чтобы установить позже с помощью /set-home в Telegram)")
            if home_channel:
                save_env_value("TELEGRAM_HOME_CHANNEL", home_channel)
    else:
        print_info("   Вы также можете установить это позже, набрав /set-home в чате Telegram.")
        home_channel = prompt("ID домашнего канала (оставьте пустым, чтобы установить позже)")
        if home_channel:
            save_env_value("TELEGRAM_HOME_CHANNEL", home_channel)


def _setup_discord():
    """Настройка учетных данных бота Discord и списка разрешенных пользователей."""
    print_header("Discord")
    existing = get_env_value("DISCORD_BOT_TOKEN")
    if existing:
        print_info("Discord: уже настроен")
        if not prompt_yes_no("Перенастроить Discord?", False):
            if not get_env_value("DISCORD_ALLOWED_USERS"):
                print_info("⚠️ В Discord не настроен список разрешенных пользователей — любой может использовать вашего бота!")
                if prompt_yes_no("Добавить разрешенных пользователей сейчас?", True):
                    print_info("   Чтобы узнать Discord ID: включите режим разработчика, нажмите правой кнопкой на имя → Копировать ID")
                    allowed_users = prompt("ID разрешенных пользователей (через запятую)")
                    if allowed_users:
                        cleaned_ids = _clean_discord_user_ids(allowed_users)
                        save_env_value("DISCORD_ALLOWED_USERS", ",".join(cleaned_ids))
                        print_success("Список разрешенных пользователей Discord настроен")
            return

    print_info("Создайте бота на https://discord.com/developers/applications")
    token = prompt("Токен бота Discord", password=True)
    if not token:
        return
    save_env_value("DISCORD_BOT_TOKEN", token)
    print_success("Токен Discord сохранен")

    print()
    print_info("🔒 Безопасность: Ограничьте тех, кто может использовать вашего бота")
    print_info("   Чтобы узнать свой Discord ID:")
    print_info("   1. Включите Режим разработчика в настройках Discord")
    print_info("   2. Нажмите правой кнопкой мыши на свое имя → Копировать ID")
    print()
    print_info("   Вы также можете использовать имена пользователей Discord (разрешаются при запуске шлюза).")
    print()
    allowed_users = prompt(
        "ID разрешенных пользователей или имена пользователей (через запятую, оставьте пустым для открытого доступа)"
    )
    if allowed_users:
        cleaned_ids = _clean_discord_user_ids(allowed_users)
        save_env_value("DISCORD_ALLOWED_USERS", ",".join(cleaned_ids))
        print_success("Список разрешенных пользователей Discord настроен")
    else:
        print_info("⚠️ Список разрешенных пользователей не установлен — любой на серверах с вашим ботом сможет им пользоваться!")

    print()
    print_info("📬 Домашний канал: куда Hermes доставляет результаты заданий cron,")
    print_info("   межплатформенные сообщения и уведомления.")
    print_info("   Чтобы получить ID канала: нажмите правой кнопкой на канал → Копировать ID канала")
    print_info("   (требуется Режим разработчика в настройках Discord)")
    print_info("   Вы также можете установить это позже, набрав /set-home в канале Discord.")
    home_channel = prompt("ID домашнего канала (оставьте пустым, чтобы установить позже с помощью /set-home)")
    if home_channel:
        save_env_value("DISCORD_HOME_CHANNEL", home_channel)


def _clean_discord_user_ids(raw: str) -> list:
    """Удаление распространенных префиксов упоминаний Discord из строки ID, разделенной запятыми."""
    cleaned = []
    for uid in raw.replace(" ", "").split(","):
        uid = uid.strip()
        if uid.startswith("<@") and uid.endswith(">"):
            uid = uid.lstrip("<@!").rstrip(">")
        if uid.lower().startswith("user:"):
            uid = uid[5:]
        if uid:
            cleaned.append(uid)
    return cleaned


def _setup_slack():
    """Настройка учетных данных бота Slack."""
    print_header("Slack")
    existing = get_env_value("SLACK_BOT_TOKEN")
    if existing:
        print_info("Slack: уже настроен")
        if not prompt_yes_no("Перенастроить Slack?", False):
            # Даже без перенастройки предлагаем обновить манифест, чтобы
            # новые команды (например, /btw, /stop и др.) зарегистрировались в Slack.
            if prompt_yes_no(
                "Перегенерировать манифест приложения Slack с последним списком команд? "
                "(рекомендуется после `hermes update`)",
                True,
            ):
                _write_slack_manifest_and_instruct()
            return

    print_info("Шаги по созданию приложения Slack:")
    print_info("   1. Перейдите на https://api.slack.com/apps → Create New App")
    print_info("      Выберите 'From an app manifest' — мы создадим его для вас ниже.")
    print_info("   2. Включите Socket Mode: Settings → Socket Mode → Enable")
    print_info("      • Создайте App-Level Token с областью действия (scope) 'connections:write'")
    print_info("   3. Установите в рабочее пространство: Settings → Install App")
    print_info("   4. После установки пригласите бота в каналы: /invite @ИмяВашегоБота")
    print()
    print_info("   Полное руководство: https://hermes-agent.nousresearch.com/docs/user-guide/messaging/slack/")
    print()

    # Генерация и запись манифеста заранее, чтобы пользователь мог вставить его в поток
    # "Create from manifest" вместо того, чтобы по очереди настраивать области действия,
    # события и слэш-команды.
    _write_slack_manifest_and_instruct()

    print()
    bot_token = prompt("Токен бота Slack (xoxb-...)", password=True)
    if not bot_token:
        return
    save_env_value("SLACK_BOT_TOKEN", bot_token)
    app_token = prompt("Токен приложения Slack (xapp-...)", password=True)
    if app_token:
        save_env_value("SLACK_APP_TOKEN", app_token)
    print_success("Токены Slack сохранены")

    print()
    print_info("🔒 Безопасность: Ограничьте тех, кто может использовать вашего бота")
    print_info("   Чтобы узнать Member ID: нажмите на имя пользователя → View full profile → ⋮ → Copy member ID")
    print()
    allowed_users = prompt(
        "ID разрешенных пользователей (через запятую, оставьте пустым, чтобы запретить доступ всем, кроме сопряженных)"
    )
    if allowed_users:
        save_env_value("SLACK_ALLOWED_USERS", allowed_users.replace(" ", ""))
        print_success("Список разрешенных пользователей Slack настроен")
    else:
        print_warning("⚠️ Список разрешенных пользователей Slack не установлен — несопряженным пользователям будет отказано по умолчанию.")
        print_info("   Установите SLACK_ALLOW_ALL_USERS=true или GATEWAY_ALLOW_ALL_USERS=true только если вы намеренно хотите открыть доступ всему рабочему пространству.")

    print()
    print_info("📬 Домашний канал: куда Hermes доставляет результаты заданий cron,")
    print_info("   межплатформенные сообщения и уведомления.")
    print_info("   Чтобы получить ID канала: откройте канал в Slack, нажмите правой кнопкой")
    print_info("   на имя канала → Copy link — ID начинается с C (например, C01ABC2DE3F).")
    print_info("   Вы также можете установить это позже, набрав /set-home в канале Slack.")
    home_channel = prompt("ID домашнего канала (оставьте пустым, чтобы установить позже с помощью /set-home)")
    if home_channel:
        save_env_value("SLACK_HOME_CHANNEL", home_channel.strip())


def _write_slack_manifest_and_instruct():
    """Генерация манифеста Slack, его запись в HERMES_HOME и вывод
    инструкций по вставке в Slack.

    Выделено в отдельную функцию, чтобы и при первоначальной настройке, и при
    отказе от перенастройки можно было обновить манифест без повторного ввода токенов.
    Ошибки не критичны — если запись манифеста не удастся, выводится предупреждение,
    а не прерывается вся настройка Slack.
    """
    try:
        from hermes_cli.slack_cli import _build_full_manifest
        from hermes_constants import get_hermes_home

        manifest = _build_full_manifest(
            bot_name="Hermes",
            bot_description="Ваш агент Hermes в Slack",
        )
        target = Path(get_hermes_home()) / "slack-manifest.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        target.write_text(
            _json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print_success(f"Манифест приложения Slack записан в: {target}")
        print_info(
            "   Вставьте его на https://api.slack.com/apps → ваше приложение → Features "
            "→ App Manifest → Edit, затем сохраните. Slack предложит "
            "переустановить приложение, если изменились области действия или слэш-команды."
        )
        print_info(
            "   Запустите `hermes slack manifest --write` в любое время, чтобы обновить "
            "список при добавлении новых команд в Hermes."
        )
    except Exception as exc:  # pragma: no cover - вспомогательная функция UX
        print_warning(f"Не удалось записать манифест Slack: {exc}")
        print_info(
            "   Вы можете сгенерировать его вручную позже с помощью: "
            "hermes slack manifest --write"
        )


def _setup_matrix():
    """Настройка учетных данных Matrix."""
    print_header("Matrix")
    existing = get_env_value("MATRIX_ACCESS_TOKEN") or get_env_value("MATRIX_PASSWORD")
    if existing:
        print_info("Matrix: уже настроен")
        if not prompt_yes_no("Перенастроить Matrix?", False):
            return

    print_info("Работает с любым сервером Matrix (Synapse, Conduit, Dendrite или matrix.org).")
    print_info("   1. Создайте бота на вашем сервере или используйте свою учетную запись")
    print_info("   2. Получите токен доступа в Element или укажите ID пользователя и пароль")
    print()
    homeserver = prompt("URL домашнего сервера (например, https://matrix.example.org)")
    if homeserver:
        save_env_value("MATRIX_HOMESERVER", homeserver.rstrip("/"))

    print()
    print_info("Аутентификация: укажите токен доступа (рекомендуется) или ID пользователя и пароль.")
    token = prompt("Токен доступа (оставьте пустым для входа по паролю)", password=True)
    if token:
        save_env_value("MATRIX_ACCESS_TOKEN", token)
        user_id = prompt("ID пользователя (@bot:server — опционально, будет определен автоматически)")
        if user_id:
            save_env_value("MATRIX_USER_ID", user_id)
        print_success("Токен доступа Matrix сохранен")
    else:
        user_id = prompt("ID пользователя (@bot:server)")
        if user_id:
            save_env_value("MATRIX_USER_ID", user_id)
        password = prompt("Пароль", password=True)
        if password:
            save_env_value("MATRIX_PASSWORD", password)
            print_success("Учетные данные Matrix сохранены")

    if token or get_env_value("MATRIX_PASSWORD"):
        print()
        want_e2ee = prompt_yes_no("Включить сквозное шифрование (E2EE)?", False)
        if want_e2ee:
            save_env_value("MATRIX_ENCRYPTION", "true")
            print_success("E2EE включено")

        matrix_pkg = "mautrix[encryption]" if want_e2ee else "mautrix"
        try:
            __import__("mautrix")
        except ImportError:
            print_info(f"Установка {matrix_pkg}...")
            import subprocess
            uv_bin = shutil.which("uv")
            if uv_bin:
                result = subprocess.run(
                    [uv_bin, "pip", "install", "--python", sys.executable, matrix_pkg],
                    capture_output=True, text=True,
                )
            else:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", matrix_pkg],
                    capture_output=True, text=True,
                )
            if result.returncode == 0:
                print_success(f"{matrix_pkg} установлен")
            else:
                print_warning(f"Установка не удалась — запустите вручную: pip install '{matrix_pkg}'")
                if result.stderr:
                    print_info(f"  Ошибка: {result.stderr.strip().splitlines()[-1]}")

        print()
        print_info("🔒 Безопасность: Ограничьте тех, кто может использовать вашего бота")
        print_info("   ID пользователя Matrix выглядит как @username:server")
        print()
        allowed_users = prompt("ID разрешенных пользователей (через запятую, оставьте пустым для открытого доступа)")
        if allowed_users:
            save_env_value("MATRIX_ALLOWED_USERS", allowed_users.replace(" ", ""))
            print_success("Список разрешенных пользователей Matrix настроен")
        else:
            print_info("⚠️ Список разрешенных пользователей не установлен — любой, кто напишет боту, сможет им пользоваться!")

        print()
        print_info("📬 Домашняя комната: куда Hermes доставляет результаты заданий cron и уведомления.")
        print_info("   ID комнат выглядят как !abc123:server (отображаются в настройках комнаты в Element)")
        print_info("   Вы также можете установить это позже, набрав /set-home в комнате Matrix.")
        home_room = prompt("ID домашней комнаты (оставьте пустым, чтобы установить позже с помощью /set-home)")
        if home_room:
            save_env_value("MATRIX_HOME_ROOM", home_room)


def _setup_mattermost():
    """Настройка учетных данных бота Mattermost."""
    print_header("Mattermost")
    existing = get_env_value("MATTERMOST_TOKEN")
    if existing:
        print_info("Mattermost: уже настроен")
        if not prompt_yes_no("Перенастроить Mattermost?", False):
            return

    print_info("Работает с любым селф-хостинг экземпляром Mattermost.")
    print_info("   1. В Mattermost: Integrations → Bot Accounts → Add Bot Account")
    print_info("   2. Скопируйте токен бота")
    print()
    mm_url = prompt("URL сервера Mattermost (например, https://mm.example.com)")
    if mm_url:
        save_env_value("MATTERMOST_URL", mm_url.rstrip("/"))
    token = prompt("Токен бота", password=True)
    if not token:
        return
    save_env_value("MATTERMOST_TOKEN", token)
    print_success("Токен Mattermost сохранен")

    print()
    print_info("🔒 Безопасность: Ограничьте тех, кто может использовать вашего бота")
    print_info("   Чтобы узнать свой ID пользователя: нажмите на аватар → Profile")
    print_info("   или используйте API: GET /api/v4/users/me")
    print()
    allowed_users = prompt("ID разрешенных пользователей (через запятую, оставьте пустым для открытого доступа)")
    if allowed_users:
        save_env_value("MATTERMOST_ALLOWED_USERS", allowed_users.replace(" ", ""))
        print_success("Список разрешенных пользователей Mattermost настроен")
    else:
        print_info("⚠️ Список разрешенных пользователей не установлен — любой, кто напишет боту, сможет им пользоваться!")

    print()
    print_info("📬 Домашний канал: куда Hermes доставляет результаты заданий cron и уведомления.")
    print_info("   Чтобы получить ID канала: нажмите на имя канала → View Info → скопируйте ID")
    print_info("   Вы также можете установить это позже, набрав /set-home в канале Mattermost.")
    home_channel = prompt("ID домашнего канала (оставьте пустым, чтобы установить позже с помощью /set-home)")
    if home_channel:
        save_env_value("MATTERMOST_HOME_CHANNEL", home_channel)
    print_info("   Открыть конфиг в редакторе:  hermes config edit")


def _setup_bluebubbles():
    """Настройка шлюза iMessage через BlueBubbles."""
    print_header("BlueBubbles (iMessage)")
    existing = get_env_value("BLUEBUBBLES_SERVER_URL")
    if existing:
        print_info("BlueBubbles: уже настроен")
        if not prompt_yes_no("Перенастроить BlueBubbles?", False):
            return

    print_info("Подключает Hermes к iMessage через BlueBubbles — бесплатный macOS-сервер")
    print_info("с открытым исходным кодом, который транслирует iMessage на любое устройство.")
    print_info("   Требуется Mac с запущенным BlueBubbles Server v1.0.0+")
    print_info("   Скачать: https://bluebubbles.app/")
    print()
    print_info("В BlueBubbles Server → Settings → API узнайте ваш URL сервера и пароль.")
    print()

    server_url = prompt("URL сервера BlueBubbles (например, http://192.168.1.10:1234)")
    if not server_url:
        print_warning("URL сервера обязателен — настройка BlueBubbles пропущена")
        return
    save_env_value("BLUEBUBBLES_SERVER_URL", server_url.rstrip("/"))

    password = prompt("Пароль сервера BlueBubbles", password=True)
    if not password:
        print_warning("Пароль обязателен — настройка BlueBubbles пропущена")
        return
    save_env_value("BLUEBUBBLES_PASSWORD", password)
    print_success("Учетные данные BlueBubbles сохранены")

    print()
    print_info("🔒 Безопасность: Ограничьте тех, кто может писать вашему боту")
    print_info("   Используйте адреса iMessage: email (user@icloud.com) или телефон (+15551234567)")
    print()
    allowed_users = prompt("Разрешенные адреса iMessage (через запятую, оставьте пустым для открытого доступа)")
    if allowed_users:
        save_env_value("BLUEBUBBLES_ALLOWED_USERS", allowed_users.replace(" ", ""))
        print_success("Список разрешенных пользователей BlueBubbles настроен")
    else:
        print_info("⚠️ Список разрешенных пользователей не установлен — любой, кто может отправить вам iMessage, сможет использовать бота!")

    print()
    print_info("📬 Домашний канал: телефон или email для доставки заданий cron и уведомлений.")
    print_info("   Вы также можете установить это позже с помощью /set-home в чате iMessage.")
    home_channel = prompt("Адрес домашнего канала (оставьте пустым, чтобы установить позже)")
    if home_channel:
        save_env_value("BLUEBUBBLES_HOME_CHANNEL", home_channel)

    print()
    print_info("Дополнительные настройки (значения по умолчанию подходят для большинства):")
    if prompt_yes_no("Настроить параметры слушателя вебхуков?", False):
        webhook_port = prompt("Порт слушателя вебхуков (по умолчанию: 8645)")
        if webhook_port:
            try:
                save_env_value("BLUEBUBBLES_WEBHOOK_PORT", str(int(webhook_port)))
                print_success(f"Порт вебхука установлен на {webhook_port}")
            except ValueError:
                print_warning("Некорректный номер порта, используется значение по умолчанию 8645")

    print()
    print_info("Для индикаторов набора текста, отчетов о прочтении и реакций tapback")
    print_info("требуется помощник BlueBubbles Private API. Базовые сообщения работают без него.")
    print_info("   Установка: https://docs.bluebubbles.app/helper-bundle/installation")


def _setup_qqbot():
    """Настройка QQ Bot (Official API v2) через конфигурацию шлюза."""
    from hermes_cli.gateway import _setup_qqbot as _gateway_setup_qqbot
    _gateway_setup_qqbot()


def _setup_webhooks():
    """Настройка интеграции через вебхуки."""
    print_header("Вебхуки")
    existing = get_env_value("WEBHOOK_ENABLED")
    if existing:
        print_info("Вебхуки: уже настроены")
        if not prompt_yes_no("Перенастроить вебхуки?", False):
            return

    print()
    print_warning("⚠ Платформы вебхуков и SMS требуют открытия портов шлюза в интернет.")
    print_warning("   В целях безопасности запускайте шлюз в изолированной среде")
    print_warning("   (Docker, VM и т.д.), чтобы ограничить радиус поражения при инъекции промпта.")
    print()
    print_info("   Полное руководство: https://hermes-agent.nousresearch.com/docs/user-guide/messaging/webhooks/")
    print()

    port = prompt("Порт вебхука (по умолчанию 8644)")
    if port:
        try:
            save_env_value("WEBHOOK_PORT", str(int(port)))
            print_success(f"Порт вебхука установлен на {port}")
        except ValueError:
            print_warning("Некорректный номер порта, используется значение по умолчанию 8644")

    secret = prompt("Глобальный HMAC-секрет (общий для всех маршрутов)", password=True)
    if secret:
        save_env_value("WEBHOOK_SECRET", secret)
        print_success("Секрет вебхука сохранен")
    else:
        print_warning("Секрет не установлен — вы должны настроить секреты для отдельных маршрутов в config.yaml")

    save_env_value("WEBHOOK_ENABLED", "true")
    print()
    print_success("Вебхуки включены! Дальнейшие шаги:")
    from hermes_constants import display_hermes_home as _dhh
    print_info(f"   1. Определите маршруты вебхуков в {_dhh()}/config.yaml")
    print_info("   2. Направьте ваш сервис (GitHub, GitLab и др.) на:")
    print_info("      http://ваш-сервер:8644/webhooks/<имя-маршрута>")
    print()
    print_info("   Руководство по настройке маршрутов:")
    print_info("   https://hermes-agent.nousresearch.com/docs/user-guide/messaging/webhooks/#configuring-routes")
    print()
    print_info("   Открыть конфиг в редакторе:  hermes config edit")


def setup_gateway(config: dict):
    """Настройка интеграции с платформами обмена сообщениями."""
    from hermes_cli.gateway import _all_platforms, _platform_status, _configure_platform

    print_header("Платформы обмена сообщениями")
    print_info("Подключитесь к мессенджерам, чтобы общаться с Hermes отовсюду.")
    print_info("Переключение пробелом, подтверждение клавишей Enter.")
    print()

    platforms = _all_platforms()

    # Построение списка, предварительный выбор уже настроенных платформ.
    items = []
    pre_selected = []
    for i, plat in enumerate(platforms):
        status = _platform_status(plat)
        items.append(f"{plat['emoji']} {plat['label']}  ({status})")
        if status == "configured":
            pre_selected.append(i)

    selected = prompt_checklist("Выберите платформы для настройки:", items, pre_selected)

    if not selected:
        print_info("Платформы не выбраны. Запустите 'hermes setup gateway' позже для настройки.")
        return

    for idx in selected:
        _configure_platform(platforms[idx])

    # ── Настройка службы шлюза ──
    # Учитываем любую платформу (встроенную или плагинную), настроенную пользователем
    # во время этого этапа — используется `_platform_status`, чтобы подхватить плагинные платформы.
    def _is_progress(status: str) -> bool:
        s = status.lower()
        return not (
            s == "not configured"
            or s.startswith("partially")
            or s.startswith("plugin disabled")
        )

    any_messaging = any(
        _is_progress(_platform_status(p)) for p in _all_platforms()
    )
    if any_messaging:
        print()
        print_info("━" * 50)
        print_success("Платформы обмена сообщениями настроены!")

        # Проверка отсутствующих домашних каналов
        missing_home = []
        if get_env_value("TELEGRAM_BOT_TOKEN") and not get_env_value(
            "TELEGRAM_HOME_CHANNEL"
        ):
            missing_home.append("Telegram")
        if get_env_value("DISCORD_BOT_TOKEN") and not get_env_value(
            "DISCORD_HOME_CHANNEL"
        ):
            missing_home.append("Discord")
        if get_env_value("SLACK_BOT_TOKEN") and not get_env_value("SLACK_HOME_CHANNEL"):
            missing_home.append("Slack")
        if get_env_value("BLUEBUBBLES_SERVER_URL") and not get_env_value("BLUEBUBBLES_HOME_CHANNEL"):
            missing_home.append("BlueBubbles")
        if get_env_value("QQ_APP_ID") and not (
            get_env_value("QQBOT_HOME_CHANNEL") or get_env_value("QQ_HOME_CHANNEL")
        ):
            missing_home.append("QQBot")

        if missing_home:
            print()
            print_warning(f"Домашний канал не установлен для: {', '.join(missing_home)}")
            print_info("   Без домашнего канала задания cron и межплатформенные")
            print_info("   сообщения не могут быть доставлены на эти платформы.")
            print_info("   Установите его позже с помощью /set-home в чате или:")
            for plat in missing_home:
                print_info(
                    f"     hermes config set {plat.upper()}_HOME_CHANNEL <ID_канала>"
                )

        # Предложение установить шлюз как системную службу
        import platform as _platform

        _is_linux = _platform.system() == "Linux"
        _is_macos = _platform.system() == "Darwin"
        _is_windows = _platform.system() == "Windows"

        from hermes_cli.gateway import (
            _is_service_installed,
            _is_service_running,
            supports_systemd_services,
            has_conflicting_systemd_units,
            has_legacy_hermes_units,
            install_linux_gateway_from_setup,
            print_systemd_scope_conflict_warning,
            print_legacy_unit_warning,
            systemd_start,
            systemd_restart,
            launchd_install,
            launchd_start,
            launchd_restart,
            UserSystemdUnavailableError,
            SystemScopeRequiresRootError,
            _system_scope_wizard_would_need_root,
            _print_system_scope_remediation,
        )

        service_installed = _is_service_installed()
        service_running = _is_service_running()
        supports_systemd = supports_systemd_services()
        supports_service_manager = supports_systemd or _is_macos or _is_windows

        print()
        if supports_systemd and has_conflicting_systemd_units():
            print_systemd_scope_conflict_warning()
            print()

        if supports_systemd and has_legacy_hermes_units():
            print_legacy_unit_warning()
            print()

        if service_running:
            if supports_systemd and _system_scope_wizard_would_need_root():
                _print_system_scope_remediation("restart")
            elif prompt_yes_no("  Перезапустить шлюз, чтобы применить изменения?", True):
                try:
                    if supports_systemd:
                        systemd_restart()
                    elif _is_macos:
                        launchd_restart()
                    elif _is_windows:
                        from hermes_cli import gateway_windows
                        gateway_windows.restart()
                except UserSystemdUnavailableError as e:
                    print_error("  Перезапуск не удался — пользовательский systemd недоступен:")
                    for line in str(e).splitlines():
                        print(f"  {line}")
                except SystemScopeRequiresRootError as e:
                    # Глубокая защита: проверка выше должна была это поймать,
                    # но гонка (появление файла юнита в процессе) может привести сюда.
                    print_error(f"  Restart failed: {e}")
                    _print_system_scope_remediation("restart")
                except Exception as e:
                    print_error(f"  Restart failed: {e}")
        elif service_installed:
            if supports_systemd and _system_scope_wizard_would_need_root():
                _print_system_scope_remediation("start")
            elif prompt_yes_no("  Запустить службу шлюза?", True):
                try:
                    if supports_systemd:
                        systemd_start()
                    elif _is_macos:
                        launchd_start()
                    elif _is_windows:
                        from hermes_cli import gateway_windows
                        gateway_windows.start()
                except UserSystemdUnavailableError as e:
                    print_error("  Запуск не удался — пользовательский systemd недоступен:")
                    for line in str(e).splitlines():
                        print(f"  {line}")
                except SystemScopeRequiresRootError as e:
                    print_error(f"  Запуск не удался: {e}")
                    _print_system_scope_remediation("start")
                except Exception as e:
                    print_error(f"  Запуск не удался: {e}")
        elif supports_service_manager:
            if supports_systemd:
                svc_name = "systemd"
            elif _is_macos:
                svc_name = "launchd"
            else:
                svc_name = "Запланированная задача"
            if prompt_yes_no(
                f"  Установить шлюз как службу {svc_name}? (работает в фоне, запускается при загрузке)",
                True,
            ):
                try:
                    installed_scope = None
                    did_install = False
                    started_inline = False
                    if supports_systemd:
                        installed_scope, did_install = install_linux_gateway_from_setup(force=False)
                    elif _is_macos:
                        launchd_install(force=False)
                        did_install = True
                    else:
                        # gateway_windows.install() регистрирует запланированную задачу
                        # И запускает ее немедленно (через schtasks /Run или прямой запуск),
                        # поэтому отдельный запрос на запуск здесь не требуется.
                        from hermes_cli import gateway_windows
                        gateway_windows.install(force=False)
                        did_install = True
                        started_inline = True
                    print()
                    if did_install and not started_inline and prompt_yes_no("  Запустить службу сейчас?", True):
                        try:
                            if supports_systemd:
                                systemd_start(system=installed_scope == "system")
                            elif _is_macos:
                                launchd_start()
                        except UserSystemdUnavailableError as e:
                            print_error("  Запуск не удался — пользовательский systemd недоступен:")
                            for line in str(e).splitlines():
                                print(f"  {line}")
                        except SystemScopeRequiresRootError as e:
                            print_error(f"  Запуск не удался: {e}")
                            _print_system_scope_remediation("start")
                        except Exception as e:
                            print_error(f"  Запуск не удался: {e}")
                except Exception as e:
                    print_error(f"  Установка не удалась: {e}")
                    print_info("  Вы можете попробовать вручную: hermes gateway install")
            else:
                print_info("  Вы можете установить позже: hermes gateway install")
                if supports_systemd:
                    print_info("  Или как службу при загрузке системы: sudo hermes gateway install --system")
                print_info("  Или запустить на переднем плане:  hermes gateway")
        else:
            from hermes_constants import is_container
            if is_container():
                print_info("Запустите шлюз, чтобы ваши боты вышли в онлайн:")
                print_info("   hermes gateway run          # Запуск как основного процесса контейнера")
                print_info("")
                print_info("Для автоматического перезапуска используйте политику перезапуска Docker:")
                print_info("   docker run --restart unless-stopped ...")
                print_info("   docker restart <контейнер>   # Ручной перезапуск")
            else:
                print_info("Запустите шлюз, чтобы ваши боты вышли в онлайн:")
                print_info("   hermes gateway              # Запуск на переднем плане")

        print_info("━" * 50)


# =============================================================================
# Раздел 5: Конфигурация инструментов (делегируется в tools_config.py)
# =============================================================================


def setup_tools(config: dict, first_install: bool = False):
    """Настройка инструментов — делегирует выполнение tools_command() в tools_config.py.

    И `hermes setup tools`, и `hermes tools` используют один и тот же поток:
    выбор платформы → переключение наборов инструментов → конфигурация провайдера/API-ключа.

    Аргументы:
        first_install: Если True, используется упрощенный поток первой установки
            (без меню платформ, запрашивает все ненастроенные API-ключи).
    """
    from hermes_cli.tools_config import tools_command

    tools_command(first_install=first_install, config=config)


# =============================================================================
# Логика пропуска разделов после миграции
# =============================================================================


def _model_section_has_credentials(config: dict) -> bool:
    """Возвращает True, если у любого известного провайдера логического вывода есть пригодные учетные данные.

    Источники истины:
      * ``PROVIDER_REGISTRY`` в ``hermes_cli.auth`` — перечисляет каждый поддерживаемый
        провайдер вместе с его ``api_key_env_vars``.
      * ``active_provider`` в хранилище аутентификации — охватывает провайдеров OAuth device-code /
        external-OAuth (Nous, Codex, Qwen, Gemini CLI, ...).
      * Устаревшие переменные окружения агрегатора OpenRouter, которые направляют общие
        значения ``OPENAI_API_KEY`` / ``OPENROUTER_API_KEY`` через OpenRouter.
    """
    try:
        from hermes_cli.auth import get_active_provider
        if get_active_provider():
            return True
    except Exception:
        pass

    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
    except Exception:
        PROVIDER_REGISTRY = {}  # type: ignore[assignment]

    def _has_key(pconfig) -> bool:
        for env_var in pconfig.api_key_env_vars:
            # CLAUDE_CODE_OAUTH_TOKEN устанавливается самим Claude Code, а не
            # пользователем — зеркально отражает is_provider_explicitly_configured в auth.py.
            if env_var == "CLAUDE_CODE_OAUTH_TOKEN":
                continue
            if get_env_value(env_var):
                return True
        return False

    # Предпочитаем провайдера, объявленного в config.yaml, чтобы избежать ложных срабатываний
    # от случайных переменных окружения (GH_TOKEN и др.), когда пользователь уже выбрал
    # другого провайдера.
    model_cfg = config.get("model") if isinstance(config, dict) else None
    if isinstance(model_cfg, dict):
        provider_id = (model_cfg.get("provider") or "").strip().lower()
        if provider_id in PROVIDER_REGISTRY:
            if _has_key(PROVIDER_REGISTRY[provider_id]):
                return True
        if provider_id == "openrouter":
            for env_var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY"):
                if get_env_value(env_var):
                    return True

    # Резервный вариант с агрегатором OpenRouter (провайдер не объявлен в конфиге).
    for env_var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        if get_env_value(env_var):
            return True

    for pid, pconfig in PROVIDER_REGISTRY.items():
        # Пропускаем copilot при автоопределении: GH_TOKEN / GITHUB_TOKEN
        # часто установлены для инструментов git. Зеркально отражает resolve_provider в auth.py.
        if pid == "copilot":
            continue
        if _has_key(pconfig):
            return True
    return False


def _gateway_platform_short_label(label: str) -> str:
    """Удаление пояснений в скобках из названия платформы шлюза."""
    base = label.split("(", 1)[0].strip()
    return base or label


def _get_section_config_summary(config: dict, section_key: str) -> Optional[str]:
    """Возвращает краткую сводку, если раздел настройки уже сконфигурирован, иначе None.

    Используется после миграции OpenClaw для определения того, какие разделы можно пропустить.
    ``get_env_value`` — это импорт на уровне модуля из hermes_cli.config,
    чтобы патчи тестов на ``setup_mod.get_env_value`` работали.
    """
    if section_key == "model":
        if not _model_section_has_credentials(config):
            return None
        model = config.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
        if isinstance(model, dict):
            return str(model.get("default") or model.get("model") or "настроено")
        return "настроено"

    elif section_key == "terminal":
        backend = cfg_get(config, "terminal", "backend", default="local")
        return f"бэкенд: {backend}"

    elif section_key == "agent":
        max_turns = cfg_get(config, "agent", "max_turns", default=90)
        return f"макс. итераций: {max_turns}"

    elif section_key == "gateway":
        from hermes_cli.gateway import _all_platforms, _platform_status
        # Учитываем любой непустой статус, кроме "not configured" —
        # такие платформы, как WhatsApp ("enabled, not paired"), Matrix ("configured
        # + E2EE") и Signal ("partially configured"), указывают на то, что пользователь
        # уже начал настройку, и нам не следует принудительно запускать раздел.
        configured = [
            _gateway_platform_short_label(plat["label"])
            for plat in _all_platforms()
            if _platform_status(plat) and _platform_status(plat) != "not configured"
        ]
        if configured:
            return ", ".join(configured)
        return None  # Платформы не настроены — раздел должен быть запущен

    elif section_key == "tools":
        tools = []
        if get_env_value("ELEVENLABS_API_KEY"):
            tools.append("TTS/ElevenLabs")
        if get_env_value("BROWSERBASE_API_KEY"):
            tools.append("Браузер")
        if get_env_value("FIRECRAWL_API_KEY"):
            tools.append("Firecrawl")
        if tools:
            return ", ".join(tools)
        return None

    return None


def _skip_configured_section(
    config: dict, section_key: str, label: str
) -> bool:
    """Показ сводки уже настроенного раздела и предложение пропустить его.

    Возвращает True, если пользователь решил пропустить, False, если раздел должен быть запущен.
    """
    summary = _get_section_config_summary(config, section_key)
    if not summary:
        return False
    print()
    print_success(f"  {label}: {summary}")
    return not prompt_yes_no(f"  Перенастроить {label.lower()}?", default=False)


# =============================================================================
# Миграция OpenClaw
# =============================================================================


_OPENCLAW_SCRIPT = (
    get_optional_skills_dir(PROJECT_ROOT / "optional-skills")
    / "migration"
    / "openclaw-migration"
    / "scripts"
    / "openclaw_to_hermes.py"
)


def _load_openclaw_migration_module():
    """Загрузка скрипта миграции openclaw_to_hermes как модуля.

    Возвращает загруженный модуль или None, если скрипт не может быть загружен.
    """
    if not _OPENCLAW_SCRIPT.exists():
        return None

    spec = importlib.util.spec_from_file_location(
        "openclaw_to_hermes", _OPENCLAW_SCRIPT
    )
    if spec is None or spec.loader is None:
        return None

    mod = importlib.util.module_from_spec(spec)
    # Регистрация в sys.modules, чтобы @dataclass мог разрешить модуль
    # (Python 3.11+ требует этого для динамически загружаемых модулей)
    import sys as _sys
    _sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        _sys.modules.pop(spec.name, None)
        raise
    return mod


# Типы элементов, представляющие высокорискованные изменения, требующие явных предупреждений.
# Токены/каналы шлюза могут перехватить платформы обмена сообщениями у старого агента.
# Значения конфигурации могут иметь разную семантику в OpenClaw и Hermes.
# Файлы инструкций/контекста (.md) могут содержать несовместимые процедуры настройки.
_HIGH_IMPACT_KIND_KEYWORDS = {
    "gateway": "⚠ Шлюз/сообщения — это настроит Hermes на использование ваших каналов сообщений OpenClaw",
    "telegram": "⚠ Telegram — это направит Hermes на вашего Telegram-бота OpenClaw",
    "slack": "⚠ Slack — это направит Hermes на ваше рабочее пространство Slack OpenClaw",
    "discord": "⚠ Discord — это направит Hermes на вашего Discord-бота OpenClaw",
    "whatsapp": "⚠ WhatsApp — это направит Hermes на ваше соединение WhatsApp OpenClaw",
    "config": "⚠ Значения конфига — настройки OpenClaw могут не соответствовать 1:1 эквивалентам в Hermes",
    "soul": "⚠ Файл инструкций — может содержать специфические для OpenClaw процедуры настройки/перезапуска",
    "memory": "⚠ Файл памяти/контекста — может ссылаться на инфраструктуру, специфичную для OpenClaw",
    "context": "⚠ Файл контекста — может содержать инструкции, специфичные для OpenClaw",
}


def _print_migration_preview(report: dict):
    """Вывод подробного предпросмотра того, что сделает миграция (сухой запуск).

    Группирует элементы по категориям и добавляет явные предупреждения для высокорискованных
    изменений, таких как захват токенов шлюза и различия в значениях конфигурации.
    """
    items = report.get("items", [])
    if not items:
        print_info("Нечего мигрировать.")
        return

    migrated_items = [i for i in items if i.get("status") == "migrated"]
    conflict_items = [i for i in items if i.get("status") == "conflict"]
    skipped_items = [i for i in items if i.get("status") == "skipped"]

    warnings_shown = set()

    if migrated_items:
        print(color("  Будет импортировано:", Colors.GREEN))
        for item in migrated_items:
            kind = item.get("kind", "unknown")
            dest = item.get("destination", "")
            if dest:
                dest_short = str(dest).replace(str(Path.home()), "~")
                print(f"      {kind:<22s} → {dest_short}")
            else:
                print(f"      {kind}")

            # Проверка высокорискованных элементов и сбор предупреждений
            kind_lower = kind.lower()
            dest_lower = str(dest).lower()
            for keyword, warning in _HIGH_IMPACT_KIND_KEYWORDS.items():
                if keyword in kind_lower or keyword in dest_lower:
                    warnings_shown.add(warning)
        print()

    if conflict_items:
        print(color("  Будет перезаписано (конфликтует с существующим конфигом Hermes):", Colors.YELLOW))
        for item in conflict_items:
            kind = item.get("kind", "unknown")
            reason = item.get("reason", "уже существует")
            print(f"      {kind:<22s}  {reason}")
        print()

    if skipped_items:
        print(color("  Будет пропущено:", Colors.DIM))
        for item in skipped_items:
            kind = item.get("kind", "unknown")
            reason = item.get("reason", "")
            print(f"      {kind:<22s}  {reason}")
        print()

    # Печать собранных предупреждений
    if warnings_shown:
        print(color("  ── Предупреждения ──", Colors.YELLOW))
        for warning in sorted(warnings_shown):
            print(color(f"    {warning}", Colors.YELLOW))
        print()
        print(color("  Примечание: значения конфигурации OpenClaw могут иметь иную семантику в Hermes.", Colors.YELLOW))
        print(color("  Например, OpenClaw tool_call_execution: \"auto\" ≠ режим yolo в Hermes.", Colors.YELLOW))
        print(color("  Файлы инструкций (.md) из OpenClaw могут содержать несовместимые процедуры.", Colors.YELLOW))
        print()


def _offer_openclaw_migration(hermes_home: Path) -> bool:
    """Обнаружение ~/.openclaw и предложение миграции во время первой настройки.

    Сначала запускает сухой запуск, чтобы показать пользователю, что именно будет импортировано,
    перезаписано или захвачено. Выполняется только после явного подтверждения.

    Возвращает True, если миграция прошла успешно, иначе False.
    """
    openclaw_dir = Path.home() / ".openclaw"
    if not openclaw_dir.is_dir():
        return False

    if not _OPENCLAW_SCRIPT.exists():
        return False

    print()
    print_header("Обнаружена установка OpenClaw")
    print_info(f"Найдены данные OpenClaw в {openclaw_dir}")
    print_info("Hermes может показать предпросмотр того, что будет импортировано, перед внесением изменений.")
    print()

    if not prompt_yes_no("Хотите посмотреть, что может быть импортировано?", default=True):
        print_info(
            "Миграция пропущена. Вы можете запустить ее позже: hermes claw migrate --dry-run"
        )
        return False

    # Убеждаемся, что config.yaml существует до того, как миграция попытается его прочитать
    config_path = get_config_path()
    if not config_path.exists():
        save_config(load_config())

    # Загрузка модуля миграции
    try:
        mod = _load_openclaw_migration_module()
        if mod is None:
            print_warning("Не удалось загрузить скрипт миграции.")
            return False
    except Exception as e:
        print_warning(f"Не удалось загрузить скрипт миграции: {e}")
        logger.debug("Ошибка загрузки модуля миграции OpenClaw", exc_info=True)
        return False

    # ── Фаза 1: Предпросмотр (сухой запуск) ──
    try:
        selected = mod.resolve_selected_options(None, None, preset="full")
        dry_migrator = mod.Migrator(
            source_root=openclaw_dir.resolve(),
            target_root=hermes_home.resolve(),
            execute=False,  # сухой запуск — файлы не изменяются
            workspace_target=None,
            overwrite=True,  # показать все, включая конфликты
            migrate_secrets=True,
            output_dir=None,
            selected_options=selected,
            preset_name="full",
        )
        preview_report = dry_migrator.migrate()
    except Exception as e:
        print_warning(f"Предпросмотр миграции не удался: {e}")
        logger.debug("Ошибка предпросмотра миграции OpenClaw", exc_info=True)
        return False

    # Отображение полного предпросмотра
    preview_summary = preview_report.get("summary", {})
    preview_count = preview_summary.get("migrated", 0)

    if preview_count == 0:
        print()
        print_info("Нечего импортировать из OpenClaw.")
        return False

    print()
    print_header(f"Предпросмотр миграции — будет импортировано элементов: {preview_count}")
    print_info("Изменения еще не внесены. Просмотрите список ниже:")
    print()
    _print_migration_preview(preview_report)

    # ── Фаза 2: Подтверждение и выполнение ──
    if not prompt_yes_no("Продолжить миграцию?", default=False):
        print_info(
            "Миграция отменена. Вы можете запустить ее позже: hermes claw migrate"
        )
        print_info(
            "Используйте --dry-run для повторного предпросмотра или --preset minimal для частичного импорта."
        )
        return False

    # Выполнение миграции — overwrite=False, чтобы сохранить существующие конфиги Hermes.
    # Пользователь видел предпросмотр; конфликты пропускаются по умолчанию.
    try:
        migrator = mod.Migrator(
            source_root=openclaw_dir.resolve(),
            target_root=hermes_home.resolve(),
            execute=True,
            workspace_target=None,
            overwrite=False,  # сохранить существующий конфиг Hermes
            migrate_secrets=True,
            output_dir=None,
            selected_options=selected,
            preset_name="full",
        )
        report = migrator.migrate()
    except Exception as e:
        print_warning(f"Миграция не удалась: {e}")
        logger.debug("Ошибка миграции OpenClaw", exc_info=True)
        return False

    # Печать итоговой сводки
    summary = report.get("summary", {})
    migrated = summary.get("migrated", 0)
    skipped = summary.get("skipped", 0)
    conflicts = summary.get("conflict", 0)
    errors = summary.get("error", 0)

    print()
    if migrated:
        print_success(f"Импортировано элементов из OpenClaw: {migrated}.")
    if conflicts:
        print_info(f"Пропущено элементов, которые уже существуют в Hermes: {conflicts} (используйте hermes claw migrate --overwrite для принудительной перезаписи).")
    if skipped:
        print_info(f"Пропущено элементов (не найдены или не изменены): {skipped}.")
    if errors:
        print_warning(f"Ошибок при миграции элементов: {errors} — проверьте отчет о миграции.")

    output_dir = report.get("output_dir")
    if output_dir:
        print_info(f"Полный отчет сохранен в: {output_dir}")

    print_success("Миграция завершена! Продолжаем настройку...")
    return True


# =============================================================================
# Основной оркестратор мастера
# =============================================================================

SETUP_SECTIONS = [
    ("model", "Модель и провайдер", setup_model_provider),
    ("tts", "Преобразование текста в речь", setup_tts),
    ("terminal", "Бэкенд терминала", setup_terminal_backend),
    ("gateway", "Платформы обмена сообщениями (шлюз)", setup_gateway),
    ("tools", "Инструменты", setup_tools),
    ("agent", "Настройки агента", setup_agent_settings),
]


def run_setup_wizard(args):
    """Запуск интерактивного мастера настройки.

    Поддерживает полную, быструю и специфичную для разделов настройку:
      hermes setup           — полная или быстрая (определяется автоматически)
      hermes setup model     — только модель/провайдер
      hermes setup tts       — только преобразование текста в речь
      hermes setup terminal  — только бэкенд терминала
      hermes setup gateway   — только платформы обмена сообщениями
      hermes setup tools     — только конфигурация инструментов
      hermes setup agent     — только настройки агента
    """
    from hermes_cli.config import is_managed, managed_error
    if is_managed():
        managed_error("запустить мастер настройки")
        return
    ensure_hermes_home()

    reset_requested = bool(getattr(args, "reset", False))
    if reset_requested:
        save_config(copy.deepcopy(DEFAULT_CONFIG))
        print_success("Конфигурация сброшена до значений по умолчанию.")

    reconfigure_requested = bool(getattr(args, "reconfigure", False))
    quick_requested = bool(getattr(args, "quick", False))

    config = load_config()
    hermes_home = get_hermes_home()

    # Резервное копирование существующей конфигурации перед тем, как настройка изменит ее (#3522)
    config_path = get_config_path()
    if config_path.exists():
        from datetime import datetime as _dt
        _backup_path = config_path.with_suffix(
            f".yaml.bak.{_dt.now().strftime('%Y%m%d_%H%M%S')}"
        )
        try:
            import shutil
            shutil.copy2(config_path, _backup_path)
        except Exception:
            _backup_path = None
    else:
        _backup_path = None

    # Определение неинтерактивных сред (безголовый SSH, Docker, CI/CD)
    non_interactive = getattr(args, 'non_interactive', False)
    if not non_interactive and not is_interactive_stdin():
        non_interactive = True

    if non_interactive:
        print_noninteractive_setup_guidance(
            "Запуск в неинтерактивной среде (TTY не обнаружен)."
        )
        return

    # Проверка, был ли запрошен конкретный раздел
    section = getattr(args, "section", None)
    if section:
        for key, label, func in SETUP_SECTIONS:
            if key == section:
                print()
                print(
                    color(
                        "┌─────────────────────────────────────────────────────────┐",
                        Colors.MAGENTA,
                    )
                )
                print(color(f"│     ⚕ Настройка Hermes — {label:<34s} │", Colors.MAGENTA))
                print(
                    color(
                        "└─────────────────────────────────────────────────────────┘",
                        Colors.MAGENTA,
                    )
                )
                func(config)
                save_config(config)
                print()
                print_success(f"Настройка раздела {label} завершена!")
                return

        print_error(f"Неизвестный раздел настройки: {section}")
        print_info(f"Доступные разделы: {', '.join(k for k, _, _ in SETUP_SECTIONS)}")
        return

    # Проверка, является ли это существующей установкой с настроенным провайдером
    from hermes_cli.auth import get_active_provider

    active_provider = get_active_provider()
    is_existing = (
        bool(get_env_value("OPENROUTER_API_KEY"))
        or bool(get_env_value("OPENAI_BASE_URL"))
        or active_provider is not None
    )

    print()
    print(
        color(
            "┌─────────────────────────────────────────────────────────┐",
            Colors.MAGENTA,
        )
    )
    print(
        color(
            "│          ⚕ Мастер настройки агента Hermes              │", Colors.MAGENTA
        )
    )
    print(
        color(
            "├─────────────────────────────────────────────────────────┤",
            Colors.MAGENTA,
        )
    )
    print(
        color(
            "│  Давайте настроим вашу установку агента Hermes.        │", Colors.MAGENTA
        )
    )
    print(
        color(
            "│  Нажмите Ctrl+C в любое время для выхода.               │", Colors.MAGENTA
        )
    )
    print(
        color(
            "└─────────────────────────────────────────────────────────┘",
            Colors.MAGENTA,
        )
    )

    migration_ran = False

    if is_existing:
        # Существующая установка — по умолчанию используется поток перенастройки полного мастера.
        # Каждое приглашение показывает текущее значение в качестве значения по умолчанию,
        # поэтому нажатие Enter сохраняет его. Выберите `--quick` для узкого потока
        # "заполнить только недостающие элементы" (полезно после частичной миграции OpenClaw
        # или когда был очищен необходимый API-ключ).
        if quick_requested:
            _run_quick_setup(config, hermes_home)
            return

        print()
        print_header("Перенастройка")
        print_success("У вас уже настроен Hermes.")
        print_info("Запуск полного мастера — каждое приглашение показывает текущее значение.")
        print_info("Нажмите Enter, чтобы сохранить его, или введите новое значение, чтобы изменить.")
        print_info("")
        print_info("Совет: переходите сразу к разделу с помощью 'hermes setup model|terminal|")
        print_info("     gateway|tools|agent' или заполните только недостающие элементы с помощью --quick.")
        # Переход к блоку "Полная настройка — запуск всех разделов" ниже.
        # --reconfigure теперь является значением по умолчанию для существующих установок; флаг
        # сохранен для обратной совместимости, но здесь не выполняет никаких действий.
    else:
        # ── Первая настройка ──
        print()

        # --reconfigure / --quick на чистой установке бессмысленны — переход к обычному первому потоку.
        if reconfigure_requested or quick_requested:
            print_info("Существующая конфигурация не найдена — запуск первой настройки.")
            print()

        # Предложение миграции OpenClaw перед началом настройки
        migration_ran = _offer_openclaw_migration(hermes_home)
        if migration_ran:
            config = load_config()

        setup_mode = prompt_choice("Как бы вы хотели настроить Hermes?", [
            "Быстрая настройка — провайдер, модель и сообщения (рекомендуется)",
            "Полная настройка — настроить всё",
        ], 0)

        if setup_mode == 0:
            _run_first_time_quick_setup(config, hermes_home, is_existing)
            return

    # ── Полная настройка — запуск всех разделов ──
    print_header("Расположение конфигурации")
    print_info(f"Файл конфигурации:  {get_config_path()}")
    print_info(f"Файл секретов:      {get_env_path()}")
    print_info(f"Папка данных:       {hermes_home}")
    print_info(f"Директория установки: {PROJECT_ROOT}")
    print()
    print_info("Вы можете редактировать эти файлы напрямую или использовать 'hermes config edit'")

    if migration_ran:
        print()
        print_info("Настройки были импортированы из OpenClaw.")
        print_info("В каждом разделе ниже будет показано, что было импортировано — нажмите Enter,")
        print_info("чтобы оставить, или выберите перенастройку при необходимости.")

    # Раздел 1: Модель и провайдер
    if not (migration_ran and _skip_configured_section(config, "model", "Модель и провайдер")):
        setup_model_provider(config)

    # Раздел 2: Бэкенд терминала
    if not (migration_ran and _skip_configured_section(config, "terminal", "Бэкенд терминала")):
        setup_terminal_backend(config)

    # Раздел 3: Настройки агента
    if not (migration_ran and _skip_configured_section(config, "agent", "Настройки агента")):
        setup_agent_settings(config)

    # Раздел 4: Платформы обмена сообщениями
    if not (migration_ran and _skip_configured_section(config, "gateway", "Платформы обмена сообщениями")):
        setup_gateway(config)

    # Раздел 5: Инструменты
    if not (migration_ran and _skip_configured_section(config, "tools", "Инструменты")):
        setup_tools(config, first_install=not is_existing)

    # Сохранение и вывод сводки
    save_config(config)
    if _backup_path and _backup_path.exists():
        print_info(f"Предыдущая конфигурация сохранена в: {_backup_path}")
        print_info("Если настройка изменила настроенное вами значение, восстановите его с помощью:")
        print_info(f"  cp {_backup_path} {config_path}")
    _print_setup_summary(config, hermes_home)

    _offer_launch_chat()


def _offer_launch_chat():
    """Предложить пользователю перейти сразу в чат после настройки."""
    print()
    if not prompt_yes_no("Запустить чат hermes сейчас?", True):
        return

    from hermes_cli.relaunch import relaunch
    relaunch(["chat"])


def _run_first_time_quick_setup(config: dict, hermes_home, is_existing: bool):
    """Упрощенная первая настройка: провайдер, модель, терминал и сообщения.

    Применяет разумные значения по умолчанию для TTS (Edge), настроек агента и инструментов —
    пользователь может настроить их позже с помощью ``hermes setup <раздел>``.
    """
    # Шаг 1: Модель и провайдер (важно — пропускает ротацию/vision/TTS)
    setup_model_provider(config, quick=True)

    # Шаг 2: Бэкенд терминала — место запуска команд является ключевым решением
    setup_terminal_backend(config)

    # Шаг 3: Применение значений по умолчанию для всего остального
    _apply_default_agent_settings(config)

    save_config(config)

    # Шаг 4: Предложение настройки шлюза сообщений
    print()
    gateway_choice = prompt_choice(
        "Подключить платформу обмена сообщениями? (Telegram, Discord и др.)",
        [
            "Настроить сообщения сейчас (рекомендуется)",
            "Пропустить — настроить позже с помощью 'hermes setup gateway'",
        ],
        0,
    )

    if gateway_choice == 0:
        setup_gateway(config)
        save_config(config)

    print()
    print_success("Настройка завершена! Вы готовы к работе.")
    print()
    print_info("  Настроить все параметры:    hermes setup")
    if gateway_choice != 0:
        print_info("  Подключить Telegram/Discord:  hermes setup gateway")
    print()

    _print_setup_summary(config, hermes_home)

    _offer_launch_chat()


def _run_quick_setup(config: dict, hermes_home):
    """Быстрая настройка — настройка только отсутствующих элементов."""
    from hermes_cli.config import (
        get_missing_env_vars,
        get_missing_config_fields,
        check_config_version,
    )

    print()
    print_header("Быстрая настройка — только недостающие элементы")

    # Проверка того, что отсутствует
    missing_required = [
        v for v in get_missing_env_vars(required_only=False) if v.get("is_required")
    ]
    missing_optional = [
        v for v in get_missing_env_vars(required_only=False) if not v.get("is_required")
    ]
    missing_config = get_missing_config_fields()
    current_ver, latest_ver = check_config_version()

    has_anything_missing = (
        missing_required
        or missing_optional
        or missing_config
        or current_ver < latest_ver
    )

    if not has_anything_missing:
        print_success("Все настроено! Делать ничего не нужно.")
        print()
        print_info("Запустите 'hermes setup' и выберите 'Полная настройка' для перенастройки,")
        print_info("или выберите конкретный раздел из меню.")
        return

    # Обработка отсутствующих обязательных переменных окружения
    if missing_required:
        print()
        print_info(f"Отсутствует {len(missing_required)} обязательных настроек:")
        for var in missing_required:
            print(f"     • {var['name']}")
        print()

        for var in missing_required:
            print()
            print(color(f"  {var['name']}", Colors.CYAN))
            print_info(f"  {var.get('description', '')}")
            if var.get("url"):
                print_info(f"  Получите ключ здесь: {var['url']}")

            if var.get("password"):
                value = prompt(f"  {var.get('prompt', var['name'])}", password=True)
            else:
                value = prompt(f"  {var.get('prompt', var['name'])}")

            if value:
                save_env_value(var["name"], value)
                print_success(f"  {var['name']} сохранено")
            else:
                print_warning(f"  {var['name']} пропущено")

    # Разделение отсутствующих опциональных переменных по категориям
    missing_tools = [v for v in missing_optional if v.get("category") == "tool"]
    missing_messaging = [
        v
        for v in missing_optional
        if v.get("category") == "messaging" and not v.get("advanced")
    ]

    # ── API-ключи инструментов (список) ──
    if missing_tools:
        print()
        print_header("API-ключи инструментов")

        checklist_labels = []
        for var in missing_tools:
            tools = var.get("tools", [])
            tools_str = f" → {', '.join(tools[:2])}" if tools else ""
            checklist_labels.append(f"{var.get('description', var['name'])}{tools_str}")

        selected_indices = prompt_checklist(
            "Какие инструменты вы хотели бы настроить?",
            checklist_labels,
        )

        for idx in selected_indices:
            var = missing_tools[idx]
            _prompt_api_key(var)

    # ── Платформы обмена сообщениями (список, затем запрос для выбранных) ──
    if missing_messaging:
        print()
        print_header("Платформы обмена сообщениями")
        print_info("Подключите Hermes к приложениям для обмена сообщениями, чтобы общаться откуда угодно.")
        print_info("Вы можете настроить их позже с помощью 'hermes setup gateway'.")

        # Группировка по платформам (с сохранением порядка)
        platform_order = []
        platforms = {}
        for var in missing_messaging:
            name = var["name"]
            if "TELEGRAM" in name:
                plat = "Telegram"
            elif "DISCORD" in name:
                plat = "Discord"
            elif "SLACK" in name:
                plat = "Slack"
            else:
                continue
            if plat not in platforms:
                platform_order.append(plat)
            platforms.setdefault(plat, []).append(var)

        platform_labels = [
            {
                "Telegram": "📱 Telegram",
                "Discord": "💬 Discord",
                "Slack": "💼 Slack",
            }.get(p, p)
            for p in platform_order
        ]

        selected_indices = prompt_checklist(
            "Какие платформы вы хотели бы настроить?",
            platform_labels,
        )

        for idx in selected_indices:
            plat = platform_order[idx]
            vars_list = platforms[plat]
            emoji = {"Telegram": "📱", "Discord": "💬", "Slack": "💼"}.get(plat, "")
            print()
            print(color(f"  ─── {emoji} {plat} ───", Colors.CYAN))
            print()
            for var in vars_list:
                print_info(f"  {var.get('description', '')}")
                if var.get("url"):
                    print_info(f"  {var['url']}")
                if var.get("password"):
                    value = prompt(f"  {var.get('prompt', var['name'])}", password=True)
                else:
                    value = prompt(f"  {var.get('prompt', var['name'])}")
                if value:
                    save_env_value(var["name"], value)
                    print_success("  ✓ Сохранено")
                else:
                    print_warning("  Пропущено")
                print()

    # Обработка отсутствующих полей конфигурации
    if missing_config:
        print()
        print_info(
            f"Добавление {len(missing_config)} новых опций конфигурации со значениями по умолчанию..."
        )
        for field in missing_config:
            print_success(f"  Добавлено {field['key']} = {field['default']}")

        # Обновление версии конфигурации
        config["_config_version"] = latest_ver
        save_config(config)

    # Переход к сводке
    _print_setup_summary(config, hermes_home)
