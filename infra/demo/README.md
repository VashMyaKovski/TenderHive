# Демонстрация TenderHive

Демонстрация запускается на отдельном стенде по опубликованной закупке хозяйственных товаров в Минске, лот 3. К письму прикладывается подготовленная PDF-выдержка с количеством 996 рулонов и требованием срока действия предложения от 60 рабочих дней. Профиль и цены поставщика, порог риска и ответы согласующих заданы для испытания. Локальная почта и Kafka проводят задание от входа до документов. Для оценки используется OpenRouter через модельный шлюз. Оператор явно подаёт нормализованные поля выдержки на этапе извлечения. [Карточка закупки №341041674](https://www.bicotender.ru/ximiia/zakupka-xoziaistvennyx-tovarov-minsk-tender341041674.html)

Из корня рабочей копии задайте в игнорируемом файле .env параметры OPENROUTER_API_KEY, OPENROUTER_MODEL_TEST, MODEL_GW_MASKING_KEY и MODEL_GW_PROVIDER=openrouter. Профиль использует openai/text-embedding-3-small по умолчанию. Через OPENROUTER_EMBEDDING_MODEL можно выбрать другую модель с той же размерностью в 1 536 измерений. Для другой модели нужен новый индекс Qdrant. Настройки в infra/demo/.env.example предназначены для локальной сети Compose.

~~~powershell
$demo = @('-p','tenderhive-demo','--env-file','.env.example','--env-file','.env','-f','compose.yaml','-f','infra/demo/compose.yaml','--profile','kb','--profile','integrations','--profile','modelgw','--profile','agents','--profile','solver','--profile','orchestrator','--profile','eval','--profile','demo')
docker compose @demo up --build --wait th-modelgw th-kb th-integrations th-agents th-solver th-orchestrator th-eval-api
docker compose @demo run --build --rm demo --seed-kb
docker compose @demo run --rm demo --output /tmp/offer
~~~

Последняя команда возвращает внешний ID письма, идентификаторы задания и трассировки и текущее состояние. После появления карточки оператор может перезапустить оркестратор и продолжить то же задание. Введите внешний ID из ответа предыдущей команды вместе с угловыми скобками.

~~~powershell
docker compose @demo restart th-orchestrator
docker compose @demo up --no-build --wait th-orchestrator
docker compose @demo run --name tenderhive-demo-output demo --external-ref '<YOUR-ID@example.test>' --approve --output /tmp/offer
docker cp tenderhive-demo-output:/tmp/offer (Join-Path $env:TEMP 'tenderhive-demo-offer')
docker rm tenderhive-demo-output
docker compose @demo down
~~~

Флаг --approve подтверждает карточки только в синтетическом сценарии с локальными учётными данными. Скрипт скачивает комплект, проверяет SHA-256 и структуру файлов. API оркестратора повторно проверяет SHA-256 перед сменой статуса и публикацией события. Скопированный комплект предназначен для локального просмотра.

## Проверка перехода на NVIDIA

Продолжайте держать запущенным стенд OpenRouter. Создайте игнорируемый файл `.env.nvidia` с `NVIDIA_API_KEY`, `MODEL_RISK`, `MODEL_EMBEDDING`, `MODEL_GW_MASKING_KEY` и `NVIDIA_DEMO_VECTOR_SIZE`. Укажите заявленную размерность выбранной модели эмбеддингов. Если модель принимает `input_type`, используйте поддерживаемое значение `MODEL_GW_EMBEDDING_INPUT_TYPE`. Если модель не поддерживает этот параметр, задайте пустое значение. Команды используют локальные параметры из корневого `.env.example` и отдельного `.env.nvidia`.

~~~powershell
$nvidia = @('-p','tenderhive-nvidia-demo','--env-file','.env.example','--env-file','.env.nvidia','-f','compose.yaml','-f','infra/demo/compose.yaml','-f','infra/demo/nvidia.compose.yaml','--profile','kb','--profile','integrations','--profile','modelgw','--profile','agents','--profile','solver','--profile','orchestrator','--profile','eval','--profile','demo')
docker compose @nvidia config --quiet
docker compose @nvidia up --build --wait th-modelgw th-kb th-integrations th-agents th-solver th-orchestrator th-eval-api
docker compose @nvidia run --build --rm demo --seed-kb

$kb = Invoke-RestMethod 'http://127.0.0.1:18295/api/v1/context?version=kb-demo-v1&query=refuse%20bags'
$kb.corpus_fragments | Select-Object fragment_id,source
$collection = Invoke-RestMethod 'http://127.0.0.1:16345/collections/th_kb_nvidia_demo'
$collection.result.config.params.vectors.size

docker compose @nvidia run --rm demo --output /tmp/offer
~~~

Начальная загрузка заново векторизует исходный корпус через NVIDIA. Активация проходит после проверки `th-eval`. `th-kb` проверяет размерность ответов шлюза при записи и поиске. Сверьте указанную размерность с выводом Qdrant и проверьте, что результат поиска включает `demo-corpus` со ссылкой на карточку закупки. Для других локальных портов используйте `NVIDIA_DEMO_KB_HOST_PORT` и `NVIDIA_DEMO_QDRANT_HOST_PORT` и подставьте их в URL.

Дождитесь `WAITING_HUMAN`, перезапустите оркестратор и завершите сценарий тем же внешним ID письма и флагом `--approve`. Используйте `@nvidia` и отдельное имя контейнера результата в командах копирования. Сравните итог `DONE`, результаты проверки `th-eval`, комплект файлов и источники с прогоном OpenRouter. В журнале шлюза проверьте успешную оценку риска и эмбеддинги с ожидаемыми идентификаторами моделей.

После успешной проверки используйте весь проект `tenderhive-nvidia-demo` как единый стенд. Имена сервиса шлюза и коллекции заданы одним набором файлов Compose. Базы, сеть, почта и индекс этого проекта изолированы. Для возврата продолжайте использовать проект OpenRouter. Команда `docker compose @nvidia down` останавливает только NVIDIA и сохраняет её данные.
