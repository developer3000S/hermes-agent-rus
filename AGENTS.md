# Агент Гермес — Руководство по разработке

Инструкции для ИИ-помощников в кодинге и разработчиков, работающих над кодовой базой hermes-agent.

## Среда разработки

```bash
# Предпочтительно .venv; используйте venv, если это то, что есть в вашем каталоге.
source .venv/bin/activate   # или: source venv/bin/activate
```

`scripts/run_tests.sh` сначала проверяет `.venv`, затем `venv`, затем
`$HOME/.hermes/hermes-agent/venv` (для рабочих деревьев, разделяющих venv с
основным каталогом).

## Структура проекта

Количество файлов постоянно меняется — не рассматривайте дерево ниже как исчерпывающее.
Каноническим источником является файловая система. В примечаниях указаны основные
точки входа, которые вы действительно будете редактировать.

```
hermes-agent/
├── run_agent.py          # Класс AIAgent — основной цикл разговора (~12k строк)
├── model_tools.py        # Оркестрация инструментов, discover_builtin_tools(), handle_function_call()
├── toolsets.py           # Определения наборов инструментов, список _HERMES_CORE_TOOLS
├── cli.py                # Класс HermesCLI — оркестратор интерактивного CLI (~11k строк)
├── hermes_state.py       # SessionDB — хранилище сессий SQLite (поиск FTS5)
├── hermes_constants.py   # get_hermes_home(), display_hermes_home() — пути с учетом профилей
├── hermes_logging.py     # setup_logging() — логи агента / ошибок / шлюза
├── batch_runner.py       # Параллельная пакетная обработка
├── agent/                # Внутренние компоненты (адаптеры провайдеров, память, кэширование и т.д.)
├── hermes_cli/           # Подкоманды CLI, мастер настройки, загрузчик плагинов, движок скинов
├── tools/                # Реализации инструментов — автообнаружение через tools/registry.py
│   └── environments/     # Бэкенды терминала (local, docker, ssh, modal, daytona, singularity)
├── gateway/              # Шлюз сообщений — run.py + session.py + platforms/
│   ├── platforms/        # Адаптеры для платформ (telegram, discord, slack, whatsapp, ...).
│   └── builtin_hooks/    # Точка расширения для всегда зарегистрированных хуков шлюза
├── plugins/              # Система плагинов
│   ├── memory/           # Плагины провайдеров памяти (honcho, mem0, supermemory, ...)
│   ├── context_engine/   # Плагины движков контекста
│   ├── model-providers/  # Плагины бэкендов инференса (openrouter, anthropic, gmi, ...)
│   ├── kanban/           # Диспетчер доски + плагин воркера
│   ├── hermes-achievements/  # Отслеживание достижений (геймификация)
│   ├── observability/    # Плагин метрик / трассировок / логов
│   ├── image_gen/        # Провайдеры генерации изображений
│   └── <others>/         # очистка диска, пример дашборда, spotify, ...
├── optional-skills/      # Тяжелые/нишевые навыки, НЕ активные по умолчанию
├── skills/               # Встроенные навыки, поставляемые с репозиторием
├── ui-tui/               # Терминальный UI на Ink (React) — `hermes --tui`
│   └── src/              # entry.tsx, app.tsx, gatewayClient.ts + компоненты/хуки
├── tui_gateway/          # Бэкенд JSON-RPC на Python для TUI
├── acp_adapter/          # Сервер ACP (интеграция с VS Code / Zed / JetBrains)
├── cron/                 # Планировщик — jobs.py, scheduler.py
├── environments/         # Среды обучения RL (Atropos)
├── scripts/              # run_tests.sh, release.py, вспомогательные скрипты
├── website/              # Сайт документации Docusaurus
└── tests/                # Набор тестов Pytest (~17k тестов)
```

**Конфиг пользователя:** `~/.hermes/config.yaml` (настройки), `~/.hermes/.env` (только API-ключи).
**Логи:** `~/.hermes/logs/` — `agent.log` (INFO+), `errors.log` (WARNING+),
`gateway.log` при запуске шлюза. Просмотр через `hermes logs [--follow]`.

---

## Класс AIAgent (run_agent.py)

`AIAgent.__init__` принимает около 60 параметров. Ниже приведен минимальный набор,
с которым вы обычно будете работать.

```python
class AIAgent:
    def __init__(self,
        base_url: str = None,
        api_key: str = None,
        provider: str = None,
        api_mode: str = None,              # "chat_completions" | "codex_responses"
        model: str = "",                   # пусто → разрешается позже из конфига
        max_iterations: int = 90,          # итерации вызова инструментов
        enabled_toolsets: list = None,
        disabled_toolsets: list = None,
        quiet_mode: bool = False,
        save_trajectories: bool = False,
        platform: str = None,              # "cli", "telegram" и т.д.
        session_id: str = None,
        skip_context_files: bool = False,
        skip_memory: bool = False,
        credential_pool=None,
    ): ...

    def chat(self, message: str) -> str:
        """Простой интерфейс — возвращает строку итогового ответа."""

    def run_conversation(self, user_message: str, system_message: str = None,
                         conversation_history: list = None, task_id: str = None) -> dict:
        """Полный интерфейс — возвращает словарь с final_response + сообщениями."""
```

---

## Архитектура CLI (cli.py)

- **Rich** для баннеров/панелей, **prompt_toolkit** для ввода с автодополнением.
- **KawaiiSpinner** (`agent/display.py`) — анимированные лица во время вызовов API.
- **Движок скинов** (`hermes_cli/skin_engine.py`) — кастомизация визуальной части CLI через конфиг `display.skin`.
- `process_command()` — метод `HermesCLI`, распределяющий команды из центрального реестра.
- Слэш-команды навыков: `agent/skill_commands.py` сканирует `~/.hermes/skills/` и внедряет их как **сообщения пользователя**.

### Реестр слэш-команд (`hermes_cli/commands.py`)

Все слэш-команды определены в центральном списке `COMMAND_REGISTRY`. Все потребители
автоматически получают данные из этого реестра.

---

## Архитектура TUI (ui-tui + tui_gateway)

TUI — это полная замена классическому CLI, активируемая через `hermes --tui`.

### Модель процесса

```
hermes --tui
  └─ Node (Ink)  ──stdio JSON-RPC──  Python (tui_gateway)
       │                                  └─ AIAgent + инструменты + сессии
       └─ отрисовывает транскрипт, поле ввода, подсказки, активность
```

---

## Добавление новых инструментов

Для кастомных инструментов используйте систему плагинов: создайте `~/.hermes/plugins/<имя>/plugin.yaml`
и зарегистрируйте инструменты через `ctx.register_tool(...)`.

Если вы добавляете инструмент в ядро:

**1. Создайте `tools/your_tool.py`:**
```python
import json, os
from tools.registry import registry

def example_tool(param: str, task_id: str = None) -> str:
    return json.dumps({"success": True, "data": "..."})

registry.register(
    name="example_tool",
    toolset="example",
    schema={"name": "example_tool", "description": "...", "parameters": {...}},
    handler=lambda args, **kw: example_tool(param=args.get("param", ""), task_id=kw.get("task_id")),
)
```

**2. Добавьте в `toolsets.py`** — либо в `_HERMES_CORE_TOOLS`, либо в новый набор.
Это необходимо, чтобы инструмент был доступен агенту.

---

## Конфигурация

- `config.yaml` — основные настройки. Добавляйте в `DEFAULT_CONFIG` в `hermes_cli/config.py`.
- `.env` — только СЕКРЕТЫ (ключи API). Добавляйте в `OPTIONAL_ENV_VARS` в `hermes_cli/config.py`.

---

## Важные политики

### Не ломайте кэширование промптов
Гермес следит за тем, чтобы кэш оставался валидным. НЕ меняйте системные промпты или наборы инструментов в середине разговора. Единственное исключение — сжатие контекста.

### Не используйте жестко заданные пути `~/.hermes`
Используйте `get_hermes_home()` для кода и `display_hermes_home()` для сообщений пользователю. Это необходимо для корректной работы профилей.

### Тестирование
**ВСЕГДА используйте `scripts/run_tests.sh`**. Не вызывайте `pytest` напрямую, чтобы гарантировать идентичность среды с CI.
