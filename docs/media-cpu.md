# CPU media pipeline

Runtime использует только ONNX Runtime CPUExecutionProvider для E5/CLIP и native
Tesseract для OCR. Torch, Transformers и SentenceTransformers отсутствуют в
пользовательском окружении. Эталонные проверки находятся в отдельном uv-проекте
validation, с CPU Torch; веса и оба окружения исключены из Git.

## Закреплённые модели

CLIP использует [multilingual text encoder](https://huggingface.co/sentence-transformers/clip-ViT-B-32-multilingual-v1)
revision `58edf8cada9e398793dca955574a48cbb7f18be2` и опубликованный
[ViT-B/32 image ONNX encoder](https://huggingface.co/Xenova/clip-vit-base-patch32)
revision `d15189d7028b43f1d3e65039190477f6af591c2a`.
Manifest `config/media_models.json` закрепляет все файлы, размеры и SHA-256.
Text: DistilBERT, mean pooling по attention mask, learned 768→512 projection без
bias, L2. Image: RGB/EXIF, PIL bicubic shortest-edge224, center-crop224, pinned
normalization, learned visual projection в графе, L2. Пространство CLIP не
смешивается с E5. Запросы CLIP ограничены128 токенами, E5 OCR-части —480 токенами.
Исходное фото ограничено32 МиБ/25M пикселей; промежуточный resize —8M пикселей и
стороной32768, чтобы узкое изображение не вызвало огромное выделение памяти.

OCR uses [tesserocr](https://github.com/sirfz/tesserocr), dictionaries from
[tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast), revision
`87416418657359cb625c412a48b6e1d6d41c29bd`. Dictionaries are Apache2.0; multilingual
CLIP is Apache2.0; original OpenAI CLIP is MIT. Pinned model cards/license files
are retained with downloaded bundles. `config/ocr_model.json` pins traineddata
and license checksums. Linux wheel tested here includes Tesseract5.5.1/Leptonica1.85.0;
Windows uv sources pin upstream standalone wheels with Tesseract5.5.3/Leptonica.
Portable builds dispatch the isolated worker through their own executable;
IPC uses ASCII JSON to preserve Russian text with Windows console encodings.
Bundled dictionaries are checksum-verified before offline copying into workspace.
The native packaging matrix and artifact smoke reports are described in
[portable builds](portable-builds.md); physical macOS checks remain separate work.

## Кэш и восстановление

SQLite migration004 хранит OCR FTS, состояния моделей, mapping media embeddings и
durable cleanup. OCR ключ: SHA файла, словари, версии binding/native runtime/Pillow,
языки и preprocessing. CLIP ключ включает оба manifests, runtime и preprocessing.
Распознавание идёт в отдельном subprocess с timeout и bounded input/output. Никакие
пути источников и распознанные тексты не выводятся в логи.

OCR сохраняет оригинальные Unicode ranges для длинного текста; E5 embeddings
публикуются после всех батчей изображения. Проверки pause/model/refs выполняются
перед каждым батчем и публикацией. Остановка не превращается в постоянную ошибку.
Failure OCR-dense учитывает версию OCR, чтобы новая версия могла повторить работу.
После сбоя батчи перезаписываются стабильными IDs. Recovery metadata пространства
коммитится до записи Lance, поэтому crash не теряет сведения для удаления векторов.

Поиск допускает только embeddings с актуальным SHA/версией/пространством и готовой
канонической photo ref. Все chat/author/date/content фильтры применяются к одному
оригинальному сообщению до top-K. Одинаковое фото может вернуть несколько источников
в разных диалогах. OCR evidence не заменяет оригинальный текст сообщения.
Медиа candidates ограничены настройкой; vectors/cleanup IDs читаются батчами512.

Все ONNX/OCR задания используют общий CPU gate. Интерактивные запросы имеют приоритет
при следующем освобождении gate; уже запущенное распознавание не прерывается и может
занять до настроенного OCR timeout. OCR/CLIP/E5-индексация чередуется. Автоматическая
выгрузка моделей выполняется после простоя. Порог RAM проверяется между заданиями;
это мягкая защита, учитывающая RSS приложения, а не лимит OS или памяти OCR child.

При удалении последней ссылки кэши SHA каскадно удаляются, durable tombstone чистит
Lance. Общие фото сохраняются, пока нужны другому диалогу. Уплотнение удаляет старые
версии OCR/CLIP, неопубликованные media IDs, старые Lance snapshots и SQLite free pages.
Размеры в UI — суммы размеров файлов; освобождение SQLite оценивается по freelist.
Удаление показывает логический объём текста и shared/exclusive media; фактическое
освобождение Lance зависит от его уплотнения, исходные файлы не удаляются.

## Проверки

```sh
uv run --no-sync python validation/validate_ocr.py --workspace workspace --output docs/benchmarks/ocr-cpu-validation.json
cd validation
uv sync --locked
uv run --no-sync python validate_clip.py --workspace ../workspace --output ../docs/benchmarks/clip-onnx-validation.json
```

Результаты [CLIP](benchmarks/clip-onnx-validation.json):6 процедурных изображений,
9 bilingual/Unicode queries; final cosine≥0.99999988, maximum error<5e-7,
top3 overlap1.0;6 русских запросов о фигурах дали верный top1.
[OCR](benchmarks/ocr-cpu-validation.json):3 синтетических RU/EN скриншота, все
контрольные слова, числа и пунктуация найдены. Это проверки совместимости и чётких
скриншотов, без утверждения качества на произвольных фотографиях.
Локальная тестовая выгрузка:10 уникальных фото,10 OCR/10 CLIP готовы,8 непустых OCR
с E5 embeddings,0 ошибок. В отчётах отсутствуют содержимое архива и идентификаторы.
