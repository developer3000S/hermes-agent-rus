"""Облегченная интернационализация (i18n) для статических сообщений Гермеса, обращенных к пользователю.

Область применения (намеренно узкая): только наиболее важные статические строки, показываемые
пользователю самим Гермесом — запросы на подтверждение, некоторые ответы на слэш-команды шлюза,
уведомления об остановке/очистке. Вывод, генерируемый агентом, строки логов,
трассировки ошибок, выводы инструментов и описания слэш-команд остаются на английском языке.

Файлы каталогов находятся в ``locales/<lang>.yaml`` в корне репозитория. Каждый
каталог представляет собой плоский словарь, ключами которого являются пути через точку (например, ``approval.choose`` или
``gateway.approval_expired``). Отсутствующие ключи откатываются к английскому языку; если английский
тоже отсутствует, возвращается сам путь ключа, чтобы сломанный каталог никогда
не приводил к сбою агента.

Использование::

    from agent.i18n import t
    print(t("approval.choose_long"))                       # текущий язык
    print(t("gateway.draining", count=3))                  # отформатировано с {count}
    print(t("approval.choose_long", lang="zh"))            # явное переопределение

Порядок разрешения языка:
    1. Явный аргумент ``lang=``, переданный в :func:`t`
    2. Переменная окружения ``HERMES_LANGUAGE`` (для тестов / быстрого переопределения)
    3. ``display.language`` из config.yaml
    4. ``"en"`` (базовый уровень)

Поддерживаемые языки: en, zh, ja, de, es, fr, tr, uk. Неизвестные значения откатываются к en.
"""

from __future__ import annotations

import logging
import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES: tuple[str, ...] = (
    "en", "zh", "zh-hant", "ja", "de", "es", "fr", "tr", "uk",
    "af", "ko", "it", "ga", "pt", "ru", "hu",
)
DEFAULT_LANGUAGE = "ru"

# Принимать несколько естественных псевдонимов, чтобы пользователи, вводящие "chinese" / "zh-CN" / "jp",
# получали правильный каталог вместо молчаливого отката к английскому.
_LANGUAGE_ALIASES: dict[str, str] = {
    "english": "en", "en-us": "en", "en-gb": "en",
    # Упрощенный китайский — сюда направляются явные коды; голый "chinese" / "mandarin"
    # также по умолчанию выбирает упрощенный, так как это большая база пользователей.
    "chinese": "zh", "mandarin": "zh", "zh-cn": "zh", "zh-hans": "zh", "zh-sg": "zh",
    # Традиционный китайский — отдельный каталог. Охватывает теги локали Тайваня / Гонконга / Макао,
    # а также распространенный псевдоним "traditional".
    "traditional-chinese": "zh-hant", "traditional_chinese": "zh-hant",
    "zh-tw": "zh-hant", "zh-hk": "zh-hant", "zh-mo": "zh-hant",
    "japanese": "ja", "jp": "ja", "ja-jp": "ja",
    "german": "de", "deutsch": "de", "de-de": "de", "de-at": "de", "de-ch": "de",
    "spanish": "es", "español": "es", "espanol": "es", "es-es": "es", "es-mx": "es", "es-ar": "es",
    "french": "fr", "français": "fr", "france": "fr", "fr-fr": "fr", "fr-be": "fr", "fr-ca": "fr", "fr-ch": "fr",
    "ukrainian": "uk", "ukrainisch": "uk", "українська": "uk", "uk-ua": "uk", "ua": "uk",
    "turkish": "tr", "türkçe": "tr", "tr-tr": "tr",
    # Африкаанс — язык Южной Африки, производный от голландского; "af-ZA" — распространенный тег BCP-47.
    "afrikaans": "af", "af-za": "af",
    # Корейский
    "korean": "ko", "한국어": "ko", "ko-kr": "ko",
    # Итальянский
    "italian": "it", "italiano": "it", "it-it": "it", "it-ch": "it",
    # Ирландский (Gaeilge) — ga — это код BCP-47
    "irish": "ga", "gaeilge": "ga", "ga-ie": "ga",
    # Португальский — голый "portuguese" направляет к европейскому португальскому; pt-br
    # относится к той же семье, но отображается здесь идентично (отдельного каталога br нет).
    "portuguese": "pt", "português": "pt", "portugues": "pt",
    "pt-pt": "pt", "pt-br": "pt", "brazilian": "pt", "brasileiro": "pt",
    # Русский
    "russian": "ru", "русский": "ru", "ru-ru": "ru",
    # Венгерский
    "hungarian": "hu", "magyar": "hu", "hu-hu": "hu",
}

_catalog_cache: dict[str, dict[str, str]] = {}
_catalog_lock = threading.Lock()


def _locales_dir() -> Path:
    """Возвращает директорию, содержащую YAML-файлы локалей.

    Находится рядом с корнем репозитория, чтобы как пакетная установка, так и редактируемая
    версия могли найти ее без манипуляций с PYTHONPATH.
    """
    # agent/i18n.py -> agent/ -> repo root
    return Path(__file__).resolve().parent.parent / "locales"


def _normalize_lang(value: Any) -> str:
    """Нормализует переданное пользователем значение языка до поддерживаемого кода.

    Принимает поддерживаемые коды напрямую, общие псевдонимы (``chinese`` -> ``zh``)
    и регистронезависимые региональные теги (``zh-CN`` -> ``zh``). Возвращает
    язык по умолчанию для неизвестных значений.
    """
    if not isinstance(value, str):
        return DEFAULT_LANGUAGE
    key = value.strip().lower()
    if not key:
        return DEFAULT_LANGUAGE
    if key in SUPPORTED_LANGUAGES:
        return key
    if key in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[key]
    # Попытка отсечь суффикс региона (например, "pt-br" -> "pt" не поддерживается,
    # но "zh-CN" -> "zh" будет).
    base = key.split("-", 1)[0]
    if base in SUPPORTED_LANGUAGES:
        return base
    return DEFAULT_LANGUAGE


def _load_catalog(lang: str) -> dict[str, str]:
    """Загружает и преобразует один YAML-файл локали в словарь с ключами через точку.

    YAML-файлы могут быть вложенными для удобства чтения человеком; это создает плоское
    пространство ключей, которое ожидает :func:`t`. Кэшируется для каждого языка на время процесса.
    """
    with _catalog_lock:
        cached = _catalog_cache.get(lang)
        if cached is not None:
            return cached

    path = _locales_dir() / f"{lang}.yaml"
    if not path.is_file():
        logger.debug("i18n catalog missing for %s at %s", lang, path)
        with _catalog_lock:
            _catalog_cache[lang] = {}
        return {}

    try:
        import yaml  # PyYAML уже является зависимостью hermes
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("Failed to load i18n catalog %s: %s", path, exc)
        with _catalog_lock:
            _catalog_cache[lang] = {}
        return {}

    flat: dict[str, str] = {}
    _flatten_into(raw, "", flat)
    with _catalog_lock:
        _catalog_cache[lang] = flat
    return flat


def _flatten_into(node: Any, prefix: str, out: dict[str, str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child_key = f"{prefix}.{key}" if prefix else str(key)
            _flatten_into(value, child_key, out)
    elif isinstance(node, str):
        out[prefix] = node
    # Нестроковые и несловарные листья игнорируются — каталоги содержат только текст.


@lru_cache(maxsize=1)
def _config_language_cached() -> str | None:
    """Читает ``display.language`` из config.yaml один раз на процесс.

    Кэшируется, так как ``t()`` вызывается в горячих путях (каждый запрос на подтверждение,
    каждый ответ шлюза), и повторное чтение YAML при каждом вызове было бы расточительным.
    ``reset_language_cache()`` очищает этот кэш при изменении конфигурации во время выполнения
    (например, после мастера настройки).
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        lang = (cfg.get("display") or {}).get("language")
        if lang:
            return _normalize_lang(lang)
    except Exception as exc:
        logger.debug("Could not read display.language from config: %s", exc)
    return None


def reset_language_cache() -> None:
    """Аннулирует кэшированное разрешение языка и каталоги.

    Вызывайте после :func:`hermes_cli.config.save_config`, если работающему процессу
    нужно подхватить измененный ``display.language`` без перезапуска.
    """
    _config_language_cached.cache_clear()
    with _catalog_lock:
        _catalog_cache.clear()


def get_language() -> str:
    """Разрешает активный язык в порядке приоритета env > config > default."""
    env_lang = os.environ.get("HERMES_LANGUAGE")
    if env_lang:
        return _normalize_lang(env_lang)
    cfg_lang = _config_language_cached()
    if cfg_lang:
        return cfg_lang
    return DEFAULT_LANGUAGE


def t(key: str, lang: str | None = None, **format_kwargs: Any) -> str:
    """Переводит ключ с точками на активный язык.

    Параметры
    ----------
    key
        Путь через точку в каталоге, например, ``"approval.choose_long"``.
    lang
        Явное переопределение языка. Имеет приоритет над env + config.
    **format_kwargs
        Аргументы подстановки ``str.format`` (``t("gateway.drain", count=3)``
        ожидает запись в каталоге с плейсхолдером ``{count}``).

    Возвращает
    -------
    Переведенную строку, или английский вариант отката, если ключ отсутствует в
    целевом языке, или сам ключ, если английский тоже отсутствует.
    """
    target = _normalize_lang(lang) if lang else get_language()
    catalog = _load_catalog(target)
    value = catalog.get(key)

    if value is None and target != DEFAULT_LANGUAGE:
        # Откат к английскому вместо показа пути ключа пользователю.
        value = _load_catalog(DEFAULT_LANGUAGE).get(key)

    if value is None:
        # Крайний случай: возвращаем сам ключ. Сломанный каталог не должен
        # приводить к сбоям; он просто выглядит некрасиво, пока кто-то его не исправит.
        logger.debug("i18n miss: key=%r lang=%r", key, target)
        value = key

    if format_kwargs:
        try:
            return value.format(**format_kwargs)
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning(
                "i18n format failed for key=%r lang=%r kwargs=%r: %s",
                key, target, format_kwargs, exc,
            )
            return value
    return value


__all__ = [
    "SUPPORTED_LANGUAGES",
    "DEFAULT_LANGUAGE",
    "t",
    "get_language",
    "reset_language_cache",
]
