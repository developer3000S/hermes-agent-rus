"""Бутстрап UTF-8 для точек входа Hermes в Windows.

У Python в Windows есть две давние проблемы с кодировкой текста:

1. ``sys.stdout`` / ``sys.stderr`` привязаны к кодовой странице консоли
   (``cp1252`` в американской локали, ``cp1251`` в русской), поэтому ``print("café")``
   вызывает ошибку ``UnicodeEncodeError: 'charmap' codec can't encode character``.

2. Дочерние процессы, созданные через ``subprocess``, не знают, что нужно использовать UTF-8,
   если в их окружении не установлены ``PYTHONUTF8`` и/или ``PYTHONIOENCODING``.
   Таким образом, любой дочерний процесс Python (песочница execute_code,
   субагенты делегирования, процессы линтера и т. д.) наследует те же настройки
   cp1251 по умолчанию и сталкивается с той же UnicodeEncodeError.

Этот модуль исправляет обе проблемы *только* в Windows — POSIX системы не затрагиваются.
Его следует импортировать в самом верху каждой точки входа Hermes
(``hermes``, ``hermes-agent``, ``hermes-acp``, ``python -m gateway.run``,
``batch_runner.py``, ``cron/scheduler.py``) перед любыми другими импортами,
которые могут выполнять ввод-вывод файлов или выводить данные в stdout.

Что этот модуль делает в Windows:

  - Устанавливает ``os.environ["PYTHONUTF8"] = "1"`` (режим UTF-8 по PEP 540), чтобы
    каждый создаваемый нами дочерний процесс использовал UTF-8 для ``open()`` и stdio.
  - Устанавливает ``os.environ["PYTHONIOENCODING"] = "utf-8"`` для дополнительной
    надежности — некоторые инструменты читают это вместо или в дополнение к
    ``PYTHONUTF8``.
  - Перенастраивает ``sys.stdout`` / ``sys.stderr`` на UTF-8 в текущем процессе,
    используя API ``reconfigure()`` (Python 3.7+). Это позволяет выводить юникод
    без перезапуска интерпретатора.

Чего этот модуль НЕ делает:

  - Он не перезапускает Python с флагом ``-X utf8``, поэтому вызовы ``open()`` в
    *текущем* процессе по-прежнему по умолчанию используют кодировку локали.
    Для них необходимо явно указывать ``encoding="utf-8"`` (правило линта
    ``PLW1514`` / ``PYI058``).

Что этот модуль делает в POSIX (Linux, macOS):

  - Ничего. Системы POSIX уже используют UTF-8 по умолчанию в 99% случаев,
    и мы не хотим изменять поведение ``LANG``/``LC_*``, которое пользователи могли
    настроить намеренно.

Идемпотентность: безопасно вызывать несколько раз. ``_bootstrap_applied``
предотвращает повторную настройку.
"""

from __future__ import annotations

import os
import sys

_IS_WINDOWS = sys.platform == "win32"
_bootstrap_applied = False


def apply_windows_utf8_bootstrap() -> bool:
    """Применить бутстрап UTF-8 для Windows, если мы находимся в Windows.

    Возвращает True, если бутстрап был применен (т. е. мы в Windows и
    еще не делали этого), иначе False.

    Идемпотентно: последующие вызовы после первого ничего не делают.
    """
    global _bootstrap_applied

    if not _IS_WINDOWS:
        return False
    if _bootstrap_applied:
        return False

    # 1. Дочерние процессы наследуют эти переменные и запускаются в режиме UTF-8.
    #    Мы используем setdefault(), чтобы пользователь мог явно отказаться,
    #    установив PYTHONUTF8=0 в своем окружении.
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # 2. Перенастройка stdio текущего процесса на UTF-8. Это необходимо, так как
    #    изменения в os.environ не перепривязывают sys.stdout автоматически.
    #    ``reconfigure`` — метод TextIOWrapper, появившийся в Python 3.7.
    #
    #    errors="replace" означает, что если мы когда-либо *прочитаем* из
    #    stdin что-то, что не является UTF-8, мы получим символы замены U+FFFD
    #    вместо аварийного завершения.
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

    # stdin также перенастраивается с errors="replace".
    stdin = getattr(sys, "stdin", None)
    if stdin is not None:
        reconfigure = getattr(stdin, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    _bootstrap_applied = True
    return True


# Применяется при импорте — точкам входа достаточно просто выполнить ``import hermes_bootstrap``
# в самом верху модуля.
apply_windows_utf8_bootstrap()
