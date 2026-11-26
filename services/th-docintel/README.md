# th-docintel — обработка документов TenderHive

`th-docintel` принимает один файл из задания, извлекает текст и таблицы из PDF/DOCX, запускает Tesseract для сканов, восстанавливает строки таблиц и передаёт нормализованные блоки в C++-ядро. Ядро по gRPC выполняет нормализацию, поиск версионируемых шаблонов Ахо–Корасиком и числовой разбор. Результат содержит требования, числа, координаты источников, уверенность и явные признаки ручной проверки.

Сервис не выбирает правовой режим, не проверяет соответствие, не рассчитывает смету, не оценивает риски, не хранит базу знаний и не создаёт карточки HITL. Эти решения остаются у вызывающих `th-orchestrator`, `th-solver` и агентов. `th-solver` может передать `requirements` из ответа напрямую: у требования есть `source`, `unit`, `needs_review` и `template_set_version`; `source_ref` для обратной совместимости формируется на стороне решателя.

## API

`POST /api/v1/extract` принимает `multipart/form-data`; JSON-ответ описан в [общем контракте](../../contracts/docintel_v1.py):

| Поле | Тип | Назначение |
| --- | --- | --- |
| `job_id` | UUID | идентификатор задания |
| `file_id` | UUID | идентификатор документа из `Job.documents` |
| `template_set_version` | строка, необязательно | фиксированная версия шаблонов для воспроизводимости; без поля используется `DOCINTEL_TEMPLATE_SET_VERSION` |
| `file` | файл | PDF, DOCX, PNG, JPEG, TIFF, BMP или WEBP |

Передавайте `X-Trace-Id` для сквозной трассировки. Ответ имеет статус `OK`, `INCOMPLETE` или `REJECTED`, массивы `requirements` и `numbers`, результат таблиц и `trace_id`. Каждый фрагмент связан с исходным `file_id`, страницей, `block_id` и bbox. Для DOCX bbox равен нулю, поскольку формат не предоставляет координат страницы.

Пример ошибки: `{ "code": "TEMPLATE_NOT_FOUND", "message": "...", "trace_id": "uuid" }`. Повреждённый файл не возвращает частичный ответ. Низкая уверенность и расхождение суммы таблицы возвращаются со статусом `INCOMPLETE`, источником и предупреждением; они предназначены для `UNCLEAR_REQUIREMENTS` в оркестраторе. Автоматического юридического вывода нет.

## Контракт ядра

`proto/docintel.proto` задаёт единственный внутренний RPC `ExtractRequirements`. Python передаёт нормализованные блоки с координатами, типом и уверенностью OCR; исходные файлы в C++ не отправляются. Ядро не обращается к хранилищам и не публикует Kafka-события. Версии шаблонов кэшируются по версии и времени изменения файла; при обновлении JSON-файла кеш обновляется без пересборки C++.

При изменении protobuf-схемы обновите Python bindings командой `uv run --locked python tools/generate_proto.py`.

Пример версии находится в [`templates/ts-2026-11-1.json`](templates/ts-2026-11-1.json). Это лексические подсказки, не правила допуска и не юридические заключения. Набор содержит базовые формулировки для штрафов, сроков, обеспечения, квалификации, ресурсов и финансовых условий; новые версии добавляются отдельными файлами.

Обработка процентов нормализует 0,15 % в коэффициент 0.0015. Ограничение «не более 10 %» сохраняется отдельным числом с ролью `upper_bound`. Даты в числовом формате возвращаются в ISO-формате. Проверка таблиц выполняется только когда распознаны столбцы количества, цены и суммы; для иных таблиц `sum_check` равен `null`.

## Границы интеграции и защита данных

- `th-orchestrator` владеет этапом `extract`, повторами и эскалациями; сервис предоставляет идемпотентно вычислимый вызов без хранения состояния. Текущий демонстрационный risk API оркестратора принимает уже подготовленный фрагмент и здесь не меняется.
- `th-solver` использует `requirements` как вход продукционных правил. Извлечение не превращает отсутствие факта в отрицательное решение.
- Агент извлечения требований в `th-agents`, если он используется, потребляет результат `th-docintel`, а не дублирует PDF/OCR/числовую логику.
- `th-eval` владеет наборами регрессии; текущий CLI ещё не подключён к извлечению. `docintel_v1` и `docintel-core-benchmark` предоставляют интерфейс и измерения для такого прогона без влияния на боевой поток.
- Необезличенные изображения не пересылаются в `th-modelgw`. Пока интеграционный контур маскирования пикселей не предоставляет проверяемый безопасный вход, сомнительные сканы возвращаются как `INCOMPLETE` для ручной проверки.

## Запуск

Из корня репозитория скопируйте настройки в локальный `.env` сервиса при необходимости и запустите профиль:

```sh
docker compose --env-file .env.example --profile docintel up --build --wait th-docintel
docker compose --env-file .env.example --profile docintel run --build --rm th-docintel-tests
```

Вторая команда изолированно проверяет DOCX и OCR-изображение через запущенные Python и C++ контейнеры.

HTTP API доступен по `http://127.0.0.1:8005`, ядро gRPC остаётся только во внутренней сети Compose. Основные параметры задаются в [`.env.example`](.env.example): размер файла и распакованного DOCX, число страниц/блоков/пикселей, OCR-язык/DPI, размер текстового блока, порог уверенности, версия и кеш шаблонов, gRPC-таймауты и предел сообщений.

## Проверки

Проверки Python можно прогнать в контейнере из корня репозитория:

```sh
docker build --target test -f services/th-docintel/Dockerfile -t tenderhive-th-docintel-tests .
docker run --rm tenderhive-th-docintel-tests
```

Проверка `th-solver` вместе с общими контрактами запускается так:

```sh
docker build --target test -f services/th-solver/Dockerfile -t tenderhive-th-solver-tests .
docker run --rm tenderhive-th-solver-tests
```

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest
cmake -S cpp -B build -DBUILD_TESTING=ON
cmake --build build --parallel
ctest --test-dir build --output-on-failure
```

Локально собрать и выполнить те же C++-тесты в Debian bookworm можно командой из каталога сервиса:

```sh
docker build --target test -f cpp/Dockerfile -t tenderhive-docintel-cpp-test .
```

Бенчмарк `docintel-core-benchmark` измеряет время обработки и пиковую память при разном числе шаблонов. Для воспроизводимости задайте `DOCINTEL_TEMPLATE_DIR`, `DOCINTEL_TEMPLATE_SET_VERSION`, `DOCINTEL_CONFIDENCE_FLOOR`, `DOCINTEL_BENCHMARK_ITERATIONS`, `DOCINTEL_BENCHMARK_TEMPLATE_MULTIPLIERS`, `DOCINTEL_BENCHMARK_TEXT`, `DOCINTEL_BENCHMARK_TEXT_REPEATS` и `DOCINTEL_BENCHMARK_BLOCKS` в окружении, затем запустите `build/docintel-core-benchmark`. Результат — CSV с параметрами и измерениями.
