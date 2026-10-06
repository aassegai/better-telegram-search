# ONNX CPU: проверка и замеры

Результаты относятся к Linux x86_64, Python 3.12.3, ORT 1.30.0, tokenizers 0.23.2,
FP32 и четырём CPU threads. Это небольшие тестовые корпуса, не прогноз для миллионов сообщений.

| Проверка | E5-small | E5-base |
|---|---:|---:|
| Max absolute error vs CPU PyTorch | 9,69e-8 | 1,79e-7 |
| Minimum cosine agreement | 0,99999988 | 0,99999988 |
| Top5 overlap | 1 | 1 |
| Peak process RSS, synthetic run | 1,22 ГиБ | 1,92 ГиБ |
| Первый сегмент с холодной session | 1,78 с | 4,12 с |
| Hybrid p50 / p95 | 16,71 / 18,91 мс | 17,25 / 19,58 мс |
| Query во время индексирования p50 / p95 | 46,62 / 70,05 мс | 70,67 / 188,65 мс |

Raw numerical reports: [small](e5-small-onnx-validation.json), [base](e5-base-onnx-validation.json).
Raw relevance/resource reports: [small](e5-small-relevance.json), [base](e5-base-relevance.json).
Первый сегмент включает построение чанков, модельный cold load и запись Lance; это не
чистое время одного model.run. Повторные запросы могут попасть в bounded query cache.

Gold задан заранее в публичном [synthetic relevance fixture](../../tests/fixtures/synthetic/relevance.json).
56 сценариев: 48 смысловых перефразировок, четыре scoped author/chat/type, два длинного
сообщения и два пустой области. Corpus: 144 сообщения, три диалога, 52 чанка; максимум
480 токенов. Метрика учитывает только matched messages, не случайное попадание в
соседний UI context; для split-tail запроса также требуется найденный char range.

| Метод | Recall@10 small / base | nDCG@10 small / base |
|---|---:|---:|
| Message BM25 AND | 0,0185 / 0,0185 | 0,0185 / 0,0185 |
| E5 exact cosine | 0,9815 / 0,9815 | 0,9328 / 0,9634 |
| Chunk BM25 + E5, RRF k60 | 0,9815 / 0,9815 | 0,9328 / 0,9634 |

Набор намеренно проверяет перефразировки, поэтому он не представляет распределение
обычных словесных запросов. Здесь hybrid не улучшил dense; нельзя объявлять его лучше
по этому измерению. Это не relevance вашей приватной переписки. Cosine/RRF не являются
вероятностями; без калибровки dense возвращает близкие кандидаты и для посторонних тем.
Фильтры пустой области вернули ноль во всех режимах.

На локальной тестовой выгрузке сохранены 221 сообщение и 56 чанков в трёх UTC-сегментах,
4322 суммарных model tokens, максимум 168; содержимое/ID/авторы сюда не включены.

## Exact или ANN

[Синтетическое сравнение](ann-synthetic.json): 4096 нормализованных random FP32 vectors,
384 dimensions, IVF_FLAT, 32 partitions, nprobes8, 30 запросов. Recall@10 ANN относительно
exact: 0,51 без фильтра и 0,4233 при prefilter до 128 vectors. Engine p50 exact около
8,9 мс, ANN около 6,6–7,0 мс. Это искусственный набор и одна конфигурация ANN;
он не измеряет Telegram relevance и не доказывает непригодность других параметров.
Для текущего небольшого архива сохраняем exact, проверенный с canonical prefilters.
Перед ANN для большого архива нужно повторить end-to-end latency и filtered recall
на репрезентативных embeddings и нескольких конфигурациях.

```sh
uv run --no-sync python scripts/benchmark_semantic.py --profile small --output docs/benchmarks/e5-small-relevance.json
uv run --no-sync python scripts/benchmark_semantic.py --profile base --output docs/benchmarks/e5-base-relevance.json
uv run --no-sync python scripts/benchmark_ann.py
```
