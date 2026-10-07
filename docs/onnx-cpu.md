# ONNX runtime (CPU baseline)

> CPU baseline; устройства 0.3.0 описаны в [GPU и обновления](gpu-and-updates.md).

Пользовательский проект управляется uv и `uv.lock`; optional extra `semantic` содержит
ONNX Runtime, tokenizers, NumPy, Hugging Face Hub, LanceDB и PyArrow. Torch,
sentence-transformers, CUDA и exporter отсутствуют. Проверка `pip freeze`/metadata
входит в synthetic тесты и CI. `.venv`, веса, кэши, SQLite/Lance и частные планы
исключены из Git. Общий tracked-file checker дополнительно запрещает веса и базы.

## Модельный контракт

Поставляются upstream ONNX FP32, без экспорта при пользовательском запуске:

| Профиль | Revision | Размерность | Набор | Opset |
|---|---|---:|---:|---:|
| E5-small | `614241f622f53c4eeff9890bdc4f31cfecc418b3` | 384 | 487850043 байт | 11 |
| E5-base | `d128750597153bb5987e10b1c3493a34e5a4502a` | 768 | 1127322481 байт | 11 |

Registry [models.json](../src/telegram_search/config/models.json) хранит source,
лицензию MIT, размеры и SHA-256 каждого ONNX/tokenizer/config/model-card файла.
Bundle staging проверяет все файлы и публикуется атомарным rename. Есть retry,
возобновляемый hub cache, byte progress, explicit repair и импорт локального набора.
При открытии workspace сеть не используется; повреждение вызывает понятную ошибку.

Tokenizer читает только локальный `tokenizer.json`, использует правильный pad ID,
выдаёт int64 input IDs/mask/type IDs. Prefix: `query: ` и `passage: `. Pooling:
masked mean над `last_hidden_state`, FP32, затем L2 normalization. Тексты длиннее
лимита отвергаются, а сообщения заранее делятся на ranges; hidden truncation отсутствует.
Embedding space включает полный manifest, preprocessing/postprocessing и точные
версии ORT/tokenizers. Смена space требует явной переиндексации.

## Ресурсы

По умолчанию используется `CPUExecutionProvider`: intra-op 4, inter-op 1, sequential execution.
CUDA/CoreML выбираются явно в настройках 0.3.0; поиск на CPU не запускает GPU-сессию. Диагностика показывает
OS/архитектуру, RAM/диск, CPU cores и доступные ORT providers; по умолчанию выбран CPU.
В пользовательском runtime отключена ORT telemetry.

Одна активная CPU session, bounded query cache (64), batch по умолчанию 4.
Интерактивный запрос получает приоритет перед следующим фоновым батчем. На OOM batch
уменьшается до 1; ошибки и checkpoint сохраняются. После 300 секунд простоя session
выгружается. При прогреве нового профиля старая session suspend/unload, индексирование
ожидает, а поиск временно работает по словам. При ошибке старый профиль возобновляется.
Пользовательская пауза сохраняется при смене модели.

На синтетическом прогоне peak RSS всего процесса: около 1,22 ГиБ small и 1,92 ГиБ base.
Это измерение на данной машине, не верхняя граница. Для small рекомендуется несколько
гигабайт свободной RAM; base требует больше. ONNX не делает модель невесомой.
Текущее полное `.venv` с semantic и dev tools занимает около 559 МиБ без весов.
Для пользовательской установки dev tools можно исключить: `uv sync --locked --extra semantic --no-dev`.

## Проверка и платформы

Численное сравнение находится в отдельном developer-проекте `validation/` с собственным
`uv.lock` и `.venv`; CPU PyTorch используется только там. Основной runtime Python
в отдельном subprocess вычисляет реальные ONNX embeddings. Сравнение RU/EN/Unicode,
длинных query/passages, batch1/2/4 и padding проверяет max absolute error ≤1e-4,
cosine agreement ≥0,99999 и top5 overlap ≥0,99. Обе модели прошли на Linux x86_64.

```sh
cd validation
uv sync --locked
uv run --no-sync python validate_e5.py --workspace ../workspace --profile small --offline --output ../docs/benchmarks/e5-small-onnx-validation.json
uv run --no-sync python validate_e5.py --workspace ../workspace --profile base --offline --output ../docs/benchmarks/e5-base-onnx-validation.json
```

Первый reference запуск требует модельного cache; уберите `--offline` для загрузки
CPU safetensors/reference tokenizer. Не запускайте validation через основную `.venv`.
Фактически проверен Linux x86_64 CPU; Windows x64 и macOS ARM имеют подходящие wheels
в lock и отдельный semantic CI job, но на физических устройствах здесь не проверялись.
GPU/CoreML и независимые устройства индексации/поиска добавлены в 0.3.0; см. [описание](gpu-and-updates.md).
