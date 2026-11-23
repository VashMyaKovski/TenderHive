# Демонстрация TenderHive

Демонстрация запускается на отдельном стенде с синтетическими данными. Она использует локальный почтовый ящик, реальный Kafka и OpenRouter через модельный шлюз. Подготовленный текст передаётся явно в этап извлечения. Выдача файлов начинается после решения по карточке финального согласования.

Из корня рабочей копии задайте в игнорируемом файле .env параметры OPENROUTER_API_KEY, OPENROUTER_MODEL_TEST, OPENROUTER_EMBEDDING_MODEL, MODEL_GW_MASKING_KEY и MODEL_GW_PROVIDER=openrouter. В демонстрационном профиле Qdrant ожидает 1 536 измерений для openai/text-embedding-3-small. Настройки в infra/demo/.env.example предназначены для локальной сети Compose.

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
