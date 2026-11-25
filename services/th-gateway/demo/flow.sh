#!/usr/bin/env bash
# Сквозная проверка th-gateway по критериям готовности.
# Запуск: bash demo/flow.sh   (шлюз должен быть поднят, см. README)
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
SERVICE_KEY="${SERVICE_API_KEY:-orchestrator-dev-key}"
PASSWORD="${DEMO_PASSWORD:-tenderhive}"
WORK="tmp/demo-$(date +%s)-$$"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT
BODY="$WORK/body.json"
PASS=0
FAIL=0

field() { python -c "import json,sys;d=json.load(sys.stdin);print(d.get(sys.argv[1],''))" "$1"; }
card_id() {
  python -c "import json,sys;d=json.load(sys.stdin);print(next((c['decision_id'] for c in d if c['reason_code']==sys.argv[1] and c['decision']=='pending'),''))" "$1"
}
check() { # check <описание> <ожидаемое> <фактическое>
  if [ "$2" = "$3" ]; then PASS=$((PASS+1)); printf '  ok   %s (%s)\n' "$1" "$3";
  else FAIL=$((FAIL+1)); printf '  FAIL %s: ожидалось %s, получено %s\n' "$1" "$2" "$3"; fi
}
status_of() { curl -s -o "$BODY" -w '%{http_code}' "$@"; }
post_json() { # post_json <url> <token> <файл-с-телом>
  curl -s -o "$BODY" -w '%{http_code}' -X POST "$1" -H "Authorization: Bearer $2" \
    -H 'Content-Type: application/json' --data-binary @"$3"
}
login() { curl -fsS -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$1\",\"password\":\"$PASSWORD\"}" | field token; }

printf '== Подготовка\n'
SPECIALIST="$(login specialist)"
LAWYER="$(login lawyer)"
HEAD="$(login head)"
KBADMIN="$(login kbadmin)"
SERVICE="$(curl -fsS -X POST "$BASE/api/auth/service" -H 'Content-Type: application/json' \
  -d "{\"api_key\":\"$SERVICE_KEY\"}" | field token)"
echo "  токены получены"

MSG="<demo-$(date +%s)-$$@company.by>"
printf 'Пакет закупки: сведения о лоте 1\n' > "$WORK/tz.txt"
printf 'Форма заявки: приложение 2\n' > "$WORK/form.txt"

cat > "$WORK/wrong.json" <<'JSON'
{"decision":"approve","comment":"обход остановки процесса"}
JSON
cat > "$WORK/lawyer.json" <<'JSON'
{"decision":"approve","comment":"штраф принят с оговоркой по графику поставки"}
JSON
cat > "$WORK/head.json" <<'JSON'
{"decision":"approve","comment":"решение об участии принято руководителем"}
JSON
cat > "$WORK/repeat.json" <<'JSON'
{"decision":"reject","comment":"повторное решение по карточке"}
JSON
cat > "$WORK/kb.json" <<'JSON'
{"comment":"проверка переключения версии из журнала"}
JSON

printf '== 1. Регистрация комплекта\n'
CODE="$(status_of -X POST "$BASE/api/jobs" -H "Authorization: Bearer $SPECIALIST" \
  --form-string "source=mail" --form-string "mailbox=tender@company.by" \
  --form-string "message_id=$MSG" --form-string "profile_ref=company-default" \
  -F "files=@$WORK/tz.txt;type=text/plain" -F "files=@$WORK/form.txt;type=text/plain")"
check "комплект зарегистрирован" "201" "$CODE"
JOB="$(field job_id < "$BODY")"
STATUS="$(field status < "$BODY")"
check "статус после регистрации" "queued" "$STATUS"

printf '== 2. Повторная загрузка того же комплекта\n'
CODE="$(status_of -X POST "$BASE/api/jobs" -H "Authorization: Bearer $SPECIALIST" \
  --form-string "source=mail" --form-string "mailbox=tender@company.by" \
  --form-string "message_id=$MSG" --form-string "profile_ref=company-default" \
  -F "files=@$WORK/tz.txt;type=text/plain" -F "files=@$WORK/form.txt;type=text/plain")"
AGAIN="$(field job_id < "$BODY")"
check "ответ без создания задания" "200" "$CODE"
check "идентификатор задания совпадает" "$JOB" "$AGAIN"

printf '== 3. Шаги обработки от оркестратора\n'
cat > "$WORK/step.json" <<'JSON'
{"code":"extract","title":"Извлечение требований","state":"finished","detail":"12 требований"}
JSON
CODE="$(status_of -X POST "$BASE/api/jobs/$JOB/steps" -H "Authorization: Bearer $SERVICE" \
  -H 'Content-Type: application/json' --data-binary @"$WORK/step.json")"
check "шаг принят" "202" "$CODE"

printf '== 4. Результат с двумя основаниями остановки\n'
cat > "$WORK/result.json" <<'JSON'
{
  "kb_version": "kb-2026.11.1",
  "status": "needs_human",
  "escalations": [
    {"reason_code": "penalty_above_threshold", "detail": "Пеня 0,5 % за день просрочки превышает порог 0,3 % из конфигурации."},
    {"reason_code": "margin_below_threshold", "detail": "Маржинальность 4,1 % ниже порога 7 %."}
  ],
  "requirements": [
    {"code": "Q-01", "text": "Опыт поставки не менее двух аналогичных контрактов", "mandatory": true, "source_ref": "п. 4.2 ТЗ"},
    {"code": "Q-02", "text": "Срок действия банковской гарантии не менее 60 дней", "mandatory": true, "source_ref": "п. 6.1 проекта договора"}
  ],
  "compliance": {
    "result": "допущен частично",
    "satisfied": [{"code": "Q-01", "text": "Опыт поставки не менее двух аналогичных контрактов", "mandatory": true, "source_ref": "п. 4.2 ТЗ"}],
    "violated": [{"code": "Q-02", "text": "Срок действия банковской гарантии не менее 60 дней", "mandatory": true, "source_ref": "п. 6.1 проекта договора"}]
  },
  "resources": {
    "coverage": 0.82,
    "deficits": [{"position_code": "OKRB 26.30.11", "title": "Коммутаторы доступа", "required": 24, "available": 16, "supplier": "поставка 21 день"}]
  },
  "estimate": {"total": 184500, "margin_percent": 4.1, "margin_threshold": 7.0},
  "risks": [
    {"code": "R-01", "category": "штрафные санкции", "level": "высокий", "consequence": "пеня 0,5 % за день просрочки", "mitigation": "согласовать график поставки с логистикой"},
    {"code": "R-02", "category": "обеспечение", "level": "средний", "consequence": "гарантия 45 дней вместо 60", "mitigation": "запросить продление в банке"}
  ],
  "recommendation": {"decision": "не участвовать без снижения цены", "rationale": "маржа 4,1 % ниже порога 7 % при пене выше допустимой"},
  "explanation": {"trace": [
    {"step": 1, "rule": "R-14", "fact": "пеня 0,5 % > порога 0,3 %", "conclusion": "остановка: penalty_above_threshold"},
    {"step": 2, "rule": "R-21", "fact": "маржа 4,1 % < порога 7 %", "conclusion": "остановка: margin_below_threshold"}
  ]},
  "artifacts": [
    {"kind": "checklist", "file_name": "checklist.md", "content_type": "text/markdown", "content_part": "checklist"},
    {"kind": "estimate", "file_name": "estimate.csv", "content_type": "text/csv", "content_part": "estimate"}
  ]
}
JSON
printf '# Чек-лист требований\n- Q-01 выполнен\n- Q-02 нарушен\n' > "$WORK/checklist.md"
printf 'позиция;количество;цена\nкоммутатор;24;4200\n' > "$WORK/estimate.csv"

CODE="$(status_of -X POST "$BASE/api/jobs/$JOB/result" -H "Authorization: Bearer $SERVICE" \
  -F "result=@$WORK/result.json;type=application/json" \
  -F "checklist=@$WORK/checklist.md;filename=checklist.md;type=text/markdown" \
  -F "estimate=@$WORK/estimate.csv;filename=estimate.csv;type=text/csv")"
check "результат принят" "200" "$CODE"
check "задание ждёт решения" "waiting_human" "$(field status < "$BODY")"

printf '== 5. Карточки согласования\n'
curl -fsS "$BASE/api/jobs/$JOB/decisions" -H "Authorization: Bearer $SPECIALIST" -o "$WORK/cards.json"
COUNT="$(python -c "import json;print(len(json.load(open('$WORK/cards.json'))))")"
check "создано карточек" "2" "$COUNT"
PENALTY="$(card_id penalty_above_threshold < "$WORK/cards.json")"
MARGIN="$(card_id margin_below_threshold < "$WORK/cards.json")"
ROLE_OF_PENALTY="$(python -c "import json;print(next(c['role'] for c in json.load(open('$WORK/cards.json')) if c['reason_code']=='penalty_above_threshold'))")"
check "владелец карточки штрафа" "lawyer" "$ROLE_OF_PENALTY"

printf '== 6. Решение чужой ролью отклонено\n'
CODE="$(post_json "$BASE/api/decisions/$PENALTY" "$SPECIALIST" "$WORK/wrong.json")"
check "доступ чужой роли" "403" "$CODE"

printf '== 7. Согласование карточек\n'
CODE="$(post_json "$BASE/api/decisions/$PENALTY" "$LAWYER" "$WORK/lawyer.json")"
check "юрист согласовал штраф" "200" "$CODE"
check "задание ещё ждёт решения" "waiting_human" "$(field job_status < "$BODY")"

CODE="$(post_json "$BASE/api/decisions/$MARGIN" "$HEAD" "$WORK/head.json")"
check "руководитель согласовал маржу" "200" "$CODE"
check "задание возвращено в обработку" "processing" "$(field job_status < "$BODY")"

printf '== 8. Повторное решение по карточке отклонено\n'
CODE="$(post_json "$BASE/api/decisions/$PENALTY" "$LAWYER" "$WORK/repeat.json")"
check "повторное решение" "409" "$CODE"

printf '== 9. Финальное утверждение пакета\n'
cat > "$WORK/final.json" <<'JSON'
{
  "kb_version": "kb-2026.11.1",
  "status": "needs_human",
  "escalations": [{"reason_code": "final_approval", "detail": "Пакет заявки сформирован, требуется утверждение."}],
  "requirements": [],
  "risks": [],
  "recommendation": {"decision": "участвовать", "rationale": "пороговые условия согласованы, пакет подготовлен"},
  "artifacts": [{"kind": "application", "file_name": "application.md", "content_type": "text/markdown", "content_part": "application"}]
}
JSON
printf '# Проект заявки\nРаздел 1. Сведения о претенденте\n' > "$WORK/application.md"
CODE="$(status_of -X POST "$BASE/api/jobs/$JOB/result" -H "Authorization: Bearer $SERVICE" \
  -F "result=@$WORK/final.json;type=application/json" \
  -F "application=@$WORK/application.md;filename=application.md;type=text/markdown")"
check "финальный пакет принят" "200" "$CODE"
curl -fsS "$BASE/api/jobs/$JOB/decisions" -H "Authorization: Bearer $HEAD" -o "$WORK/cards.json"
FINAL="$(card_id final_approval < "$WORK/cards.json")"
CODE="$(post_json "$BASE/api/decisions/$FINAL" "$HEAD" "$WORK/head.json")"
check "пакет утверждён" "200" "$CODE"
check "задание завершено" "completed" "$(field job_status < "$BODY")"

printf '== 10. Артефакты и версии\n'
curl -fsS "$BASE/api/jobs/$JOB/artifacts" -H "Authorization: Bearer $SPECIALIST" -o "$WORK/artifacts.json"
COUNT="$(python -c "import json;print(len(json.load(open('$WORK/artifacts.json'))))")"
check "артефактов в задании" "3" "$COUNT"
ARTIFACT="$(python -c "import json;print(json.load(open('$WORK/artifacts.json'))[0]['artifact_id'])")"
LINK="$(curl -fsS "$BASE/api/jobs/$JOB/artifacts/$ARTIFACT/link" -H "Authorization: Bearer $SPECIALIST" | field url)"
BYTES="$(curl -fsS "$LINK" | wc -c)"
if [ "$BYTES" -gt 0 ]; then PASS=$((PASS+1)); printf '  ok   файл по подписанной ссылке (%s байт)\n' "$BYTES";
else FAIL=$((FAIL+1)); printf '  FAIL подписанная ссылка вернула пустой файл\n'; fi

printf '== 11. База знаний: активация и откат\n'
curl -fsS "$BASE/api/kb/versions" -H "Authorization: Bearer $KBADMIN" -o "$WORK/kb.json"
code_activate() { post_json "$BASE/api/kb/versions/$1/activate" "$2" "$WORK/kb.json"; }
check "откат на предыдущую версию" "200" "$(code_activate kb-2026.10.1 "$KBADMIN")"
check "возврат текущей версии" "200" "$(code_activate kb-2026.11.1 "$KBADMIN")"
check "активация без роли администратора" "403" "$(code_activate kb-2026.09.1 "$SPECIALIST")"

printf '\nИтог: успешно %s, ошибок %s\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
