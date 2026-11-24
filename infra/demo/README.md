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

## Веб-интерфейс для локального стенда

Добавьте профиль `web` к командам OpenRouter выше. Интерфейс работает с теми же экземплярами оркестратора, агентов и решателя. Соберите и запустите его из корня ветки `feat/th-web-demo`.

~~~powershell
$workbench = $demo + @('-f','infra/demo/web.compose.yaml','--profile','web')
docker compose @workbench up --build --wait th-modelgw th-kb th-agents th-solver th-orchestrator th-eval-api th-workbench
docker compose @workbench run --build --rm demo --seed-kb
~~~

Откройте http://127.0.0.1:8787. Чтобы поменять локальный порт, задайте `TH_WORKBENCH_HOST_PORT` перед запуском. Добавьте до пяти PDF или TXT, заполните параметры одного лота в BYN и укажите общедоступный фрагмент для оценки рисков. Кнопка «Заполнить пример» подставляет демонстрационные данные минского лота. Текст сканов и числовые параметры вводит оператор. Модель получает только выделенный фрагмент, а загруженные файлы хранятся в памяти локального интерфейса.

В ленте показываются запросы и ответы служб. После проверки риска и работы решателя карточка результата показывает рекомендацию, обоснование и условия. Пакет DOCX, PDF и XLSX можно скачать после итогового согласования. Файлы и лента текущего веб-прогона доступны до перезапуска контейнера интерфейса.
