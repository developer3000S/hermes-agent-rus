"""Общие константы для Hermes Agent.

Модуль, безопасный для импорта, без зависимостей — может быть импортирован
откуда угодно без риска циклических импортов.
"""

import os
from pathlib import Path


_profile_fallback_warned: bool = False


def get_hermes_home() -> Path:
    """Возвращает домашнюю директорию Hermes (по умолчанию: ~/.hermes).

    Читает переменную окружения HERMES_HOME, по умолчанию ~/.hermes.
    Это единственный источник истины — все остальные копии должны импортировать это.

    Если ``HERMES_HOME`` не установлена, но файл ``active_profile`` указывает на
    активный профиль, отличный от профиля по умолчанию, выводится громкое разовое
    предупреждение в ``errors.log``, чтобы повреждение данных между профилями
    было диагностируемым. В противном случае поведение не меняется — мы всё равно
    возвращаем ``~/.hermes``.
    См. https://github.com/developer3000S/hermes-agent-rus/issues/18594.
    """
    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        return Path(val)

    # Защита: если активен профиль, отличный от профиля по умолчанию, предупредить один раз,
    # что возврат к профилю по умолчанию, скорее всего, ошибочен.
    global _profile_fallback_warned
    if not _profile_fallback_warned:
        try:
            # Разрешение пути по умолчанию из get_default_hermes_root()
            # для обеспечения безопасности импорта.
            active_path = (Path.home() / ".hermes" / "active_profile")
            active = active_path.read_text().strip() if active_path.exists() else ""
        except (UnicodeDecodeError, OSError):
            active = ""
        if active and active != "default":
            _profile_fallback_warned = True
            # Запись напрямую в stderr. Мы намеренно НЕ направляем это через ``logging``.
            import sys
            msg = (
                f"[HERMES_HOME fallback] HERMES_HOME не установлена, но активный "
                f"профиль — {active!r}. Возврат к ~/.hermes, который является "
                f"профилем ПО УМОЛЧАНИЮ, а не {active!r}. Любые данные, записываемые "
                f"этим процессом, попадут в неверный профиль. Создатель подпроцесса "
                f"должен явно передавать HERMES_HOME (см. issue #18594)."
            )
            try:
                sys.stderr.write(msg + "\n")
                sys.stderr.flush()
            except Exception:
                pass

    return Path.home() / ".hermes"


def get_default_hermes_root() -> Path:
    """Возвращает корневую директорию Hermes для операций на уровне профиля.

    При стандартном развертывании это ``~/.hermes``.

    В Docker или кастомных развертываниях, где ``HERMES_HOME`` указывает за пределы
    ``~/.hermes`` (например, ``/opt/data``), возвращает ``HERMES_HOME`` напрямую.

    В режиме профилей, где ``HERMES_HOME`` имеет вид ``<root>/profiles/<name>``,
    возвращает ``<root>``, чтобы ``profile list`` мог видеть все профили.
    """
    native_home = Path.home() / ".hermes"
    env_home = os.environ.get("HERMES_HOME", "")
    if not env_home:
        return native_home
    env_path = Path(env_home)
    try:
        env_path.resolve().relative_to(native_home.resolve())
        # HERMES_HOME находится внутри ~/.hermes
        return native_home
    except ValueError:
        pass

    # Docker / кастомное развертывание.
    # Проверка, является ли это путем профиля: <root>/profiles/<name>
    if env_path.parent.name == "profiles":
        return env_path.parent.parent

    # Не путь профиля — HERMES_HOME сам по себе является корнем
    return env_path


def get_optional_skills_dir(default: Path | None = None) -> Path:
    """Возвращает директорию дополнительных навыков (optional-skills).

    В упакованных установках ``optional-skills`` может находиться вне дерева
    пакета Python и предоставляться через ``HERMES_OPTIONAL_SKILLS``.
    """
    override = os.getenv("HERMES_OPTIONAL_SKILLS", "").strip()
    if override:
        return Path(override)
    if default is not None:
        return default
    return get_hermes_home() / "optional-skills"


def get_hermes_dir(new_subpath: str, old_name: str) -> Path:
    """Разрешает поддиректорию Hermes с обратной совместимостью.

    Новые установки получают консолидированную структуру (например, ``cache/images``).
    Существующие установки со старым путем (например, ``image_cache``) продолжают его использовать.

    Аргументы:
        new_subpath: Предпочтительный путь относительно HERMES_HOME (например, ``"cache/images"``).
        old_name: Устаревший путь относительно HERMES_HOME (например, ``"image_cache"``).

    Возвращает:
        Абсолютный ``Path`` — старое расположение, если оно существует, иначе новое.
    """
    home = get_hermes_home()
    old_path = home / old_name
    if old_path.exists():
        return old_path
    return home / new_subpath


def display_hermes_home() -> str:
    """Возвращает удобную для пользователя строку для отображения текущего HERMES_HOME.

    Использует сокращение ``~/`` для читаемости::

        default:  ``~/.hermes``
        profile:  ``~/.hermes/profiles/coder``
        custom:   ``/opt/hermes-custom``

    Используйте это в сообщениях, **видимых пользователю**, вместо жесткого кодирования ``~/.hermes``.
    Для кода, которому нужен реальный объект ``Path``, используйте :func:`get_hermes_home`.
    """
    home = get_hermes_home()
    try:
        return "~/" + str(home.relative_to(Path.home()))
    except ValueError:
        return str(home)


def get_subprocess_home() -> str | None:
    """Возвращает домашнюю директорию HOME для подпроцессов (с учетом профиля) или None.

    Когда ``{HERMES_HOME}/home/`` существует на диске, подпроцессы должны использовать
    её в качестве ``HOME``, чтобы системные инструменты (git, ssh, gh, npm …) записывали
    свои конфиги внутри директории данных Hermes, а не в системную ``/root`` или ``~/``.
    Это обеспечивает:

    * **Постоянство в Docker** — конфиги инструментов попадают в постоянный том.
    * **Изоляцию профилей** — каждый профиль получает свою git-идентичность, SSH-ключи и т.д.
    """
    hermes_home = os.getenv("HERMES_HOME")
    if not hermes_home:
        return None
    profile_home = os.path.join(hermes_home, "home")
    if os.path.isdir(profile_home):
        return profile_home
    return None


# Допустимые уровни усилий рассуждения (reasoning effort)
VALID_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


def parse_reasoning_effort(effort: str) -> dict | None:
    """Преобразовать уровень усилий рассуждения в словарь конфигурации.

    Допустимые уровни: "none", "minimal", "low", "medium", "high", "xhigh".
    """
    if not effort or not effort.strip():
        return None
    effort = effort.strip().lower()
    if effort == "none":
        return {"enabled": False}
    if effort in VALID_REASONING_EFFORTS:
        return {"enabled": True, "effort": effort}
    return None


def is_termux() -> bool:
    """Возвращает True при работе в среде Termux (Android)."""
    prefix = os.getenv("PREFIX", "")
    return bool(os.getenv("TERMUX_VERSION") or "com.termux/files/usr" in prefix)


_wsl_detected: bool | None = None


def is_wsl() -> bool:
    """Возвращает True при работе внутри WSL (Windows Subsystem for Linux)."""
    global _wsl_detected
    if _wsl_detected is not None:
        return _wsl_detected
    try:
        with open("/proc/version", "r", encoding="utf-8") as f:
            _wsl_detected = "microsoft" in f.read().lower()
    except Exception:
        _wsl_detected = False
    return _wsl_detected


_container_detected: bool | None = None


def is_container() -> bool:
    """Возвращает True при работе внутри контейнера Docker/Podman."""
    global _container_detected
    if _container_detected is not None:
        return _container_detected
    if os.path.exists("/.dockerenv"):
        _container_detected = True
        return True
    if os.path.exists("/run/.containerenv"):
        _container_detected = True
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8") as f:
            cgroup = f.read()
            if "docker" in cgroup or "podman" in cgroup or "/lxc/" in cgroup:
                _container_detected = True
                return True
    except OSError:
        pass
    _container_detected = False
    return False


# ─── Известные пути ─────────────────────────────────────────────────────────


def get_config_path() -> Path:
    """Возвращает путь к ``config.yaml`` внутри HERMES_HOME."""
    return get_hermes_home() / "config.yaml"


def get_skills_dir() -> Path:
    """Возвращает путь к директории навыков (skills) внутри HERMES_HOME."""
    return get_hermes_home() / "skills"



def get_env_path() -> Path:
    """Возвращает путь к файлу ``.env`` внутри HERMES_HOME."""
    return get_hermes_home() / ".env"


# ─── Настройки сети ─────────────────────────────────────────────────────


def apply_ipv4_preference(force: bool = False) -> None:
    """Применить предпочтение соединений IPv4 через monkey-patching ``socket.getaddrinfo``.

    На серверах с неработающим или недоступным IPv6 Python сначала пробует записи AAAA
    и зависает до истечения тайм-аута TCP перед переходом на IPv4. Это влияет на httpx,
    requests, urllib и SDK OpenAI.

    Если *force* равно True, патчит ``getaddrinfo`` так, чтобы вызовы с
    ``family=AF_UNSPEC`` (по умолчанию) разрешались как ``AF_INET``, полностью пропуская IPv6.
    Если запись A не существует, возвращается к исходному разрешению.
    """
    if not force:
        return

    import socket

    # Защита от повторного патчинга
    if getattr(socket.getaddrinfo, "_hermes_ipv4_patched", False):
        return

    _original_getaddrinfo = socket.getaddrinfo

    def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if family == 0:  # AF_UNSPEC — вызывающий не запросил конкретное семейство
            try:
                return _original_getaddrinfo(
                    host, port, socket.AF_INET, type, proto, flags
                )
            except socket.gaierror:
                # Нет записи A — возврат к полному разрешению (хосты только с IPv6)
                return _original_getaddrinfo(host, port, family, type, proto, flags)
        return _original_getaddrinfo(host, port, family, type, proto, flags)

    _ipv4_getaddrinfo._hermes_ipv4_patched = True  # type: ignore[attr-defined]
    socket.getaddrinfo = _ipv4_getaddrinfo  # type: ignore[assignment]


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODELS_URL = f"{OPENROUTER_BASE_URL}/models"

AI_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"
