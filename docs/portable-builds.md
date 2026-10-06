# Автономные CPU-сборки

Сборка содержит Python, интерфейс, ONNX Runtime, LanceDB, tokenizer и Tesseract
с русским и английским словарями. Python, uv, Node.js и системный Tesseract для
запуска не нужны. E5 и CLIP загружаются в настройках отдельно по закреплённым SHA;
OCR подготавливается из сборки без сети. Переписки и индексы в сборку не входят.

| Платформа | Архитектура | Минимальная ОС | Формат |
| --- | --- | --- | --- |
| Windows | x86_64 | Windows 10 1903+ / Server 2022 | ZIP |
| Linux / WSL2 | x86_64 | Ubuntu 22.04+ / glibc 2.35+ для CI | tar.gz |
| Linux | ARM64 | Ubuntu 22.04+ / glibc 2.35+ для CI | tar.gz |
| macOS | Apple Silicon | macOS 15 | ZIP с .app |
| macOS | Intel | macOS 15 | ZIP с .app |

Это целевая матрица; готовность конкретного артефакта подтверждается его JSON-отчётом
с десятью успешными проверками. Windows ARM64 пока без отдельной нативной сборки.
Linux-пакет этой рабочей машины может требовать более новую glibc; переносимая
сборка из CI создаётся на Ubuntu 22.04. Подпись Windows и нотарификация macOS
не настроены; macOS использует локальную ad-hoc подпись PyInstaller.

Распакуйте **всю папку**, сохраняя вложенные файлы. В Windows запустите
`BetterTelegramSearch/telegram-search.exe`; в Linux — `BetterTelegramSearch/telegram-search`;
в macOS откройте `Better Telegram Search.app`. Браузер откроется после готовности
локального сервера. Закройте процесс/терминал приложения, чтобы остановить сервер.
Если порт 8765 занят, используйте `telegram-search run --port 8767`.

Данные хранятся отдельно от приложения:

- Windows: `%LOCALAPPDATA%\BetterTelegramSearch\workspace`.
- macOS: `~/Library/Application Support/BetterTelegramSearch/workspace`.
- Linux: `${XDG_DATA_HOME:-~/.local/share}/BetterTelegramSearch/workspace`.

Для существующего архива: `telegram-search --workspace /path/to/workspace run`.
Перед обновлением остановите старую версию; новую распакуйте в новую папку.
Переносимый архив и JSON сопровождаются SHA-256. `telegram-search --self-test`
проверяет сборку офлайн на синтетических данных во временной папке.

## Воспроизведение сборки

На целевой ОС нужны uv, Git и Node.js 22. Отдельное окружение сборщика не содержит
PyTorch, transformers или sentence-transformers:

```sh
UV_PROJECT_ENVIRONMENT=workspace/build-env uv sync --locked --no-dev --group bundle --extra semantic --extra ocr --python 3.12 --python-preference only-managed
cd frontend
npm ci
cd ..
workspace/build-env/bin/python scripts/build_app.py --expected-arch x86_64
```

В PowerShell сначала задайте `$env:UV_PROJECT_ENVIRONMENT = 'workspace/build-env'`,
затем выполните ту же команду `uv sync` без префикса переменной окружения.
Сборщик запускается через `workspace/build-env/Scripts/python.exe`.
Для ARM64 укажите `--expected-arch arm64`.

Результаты лежат в игнорируемой папке `artifacts/`. Сборщик распаковывает готовый
архив вне репозитория и проверяет SQLite FTS5, LanceDB, CPU ONNX, tokenizer,
safetensors, OCR rus/eng, HTML/API и защиту мутаций токеном сессии.
Распаковка и workspace используют кириллицу, китайские символы и пробелы;
Windows smoke проверяет process code page UTF-8 для native OCR путей.
macOS Intel закрепляет последние совместимые wheels ONNX Runtime 1.23.2 и
LanceDB 0.25.3; версии остальных платформ определяет общий uv.lock.
Лицензии зависимостей и их версии входят в `_internal/licenses` (в macOS — ресурсы
`.app`). Native-зависимости требуют сборки на каждой ОС/архитектуре отдельно.

CI `Portable builds` собирает пять пакетов. Они загружаются в **черновик релиза**;
он не публикуется автоматически. Ветка `build-status` содержит JSON-отчёт
`latest.json` со статусами платформ, SHA-256 и ссылками на скачивание.
Черновик доступен пользователям с доступом к репозиторию. Проверка содержимого
Git выполняется до сборки; бинарные файлы, планы, модели и архивы остаются вне Git.
