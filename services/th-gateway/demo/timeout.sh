#!/usr/bin/env bash
# Проверка таймаута карточки согласования.
# Требует короткого срока ожидания: HITL_TIMEOUT_HOURS=0.002 (около 7 секунд) в .env шлюза.
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
SERVICE_KEY="${SERVICE_API_KEY:-orchestrator-dev-key}"
PASSWORD="${DEMO_PASSWORD:-tenderhive}"
WORK="tmp/demo-timeout-$(date +%s)-$$"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

field() { python -c "import json,sys;d=json.load(sys.stdin);print(d.get(sys.argv[1],''))" "$1"; }
login() { curl -fsS -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$1\",\"password\":\"$PASSWORD\"}" | field token; }

SPECIALIST="$(login specialist)"
SERVICE="$(curl -fsS -X POST "$BASE/api/auth/service" -H 'Content-Type: application/json' \
  -d "{\"api_key\":\"$SERVICE_KEY\"}" | field token)"

printf 'Комплект для проверки таймаута\n' > "$WORK/tz.txt"
curl -fsS -X POST "$BASE/api/jobs" -H "Authorization: Bearer $SPECIALIST" \
  --form-string "source=web" --form-string "mailbox=demo@company.by" \
  --form-string "message_id=<timeout-$RANDOM@company.by>" \
  -F "files=@$WORK/tz.txt;type=text/plain" > "$WORK/job.json"
JOB="$(field job_id < "$WORK/job.json")"
echo "задание: $JOB"

cat > "$WORK/result.json" <<'JSON'
{
  "status": "needs_human",
  "escalations": [{"reason_code": "resource_gap", "detail": "Склад не покрывает 8 коммутаторов, поставка 21 день."}],
  "requirements": [],
  "risks": [],
  "recommendation": {"decision": "уточнить снабжение", "rationale": "дефицит ресурса подтверждён"}
}
JSON
curl -fsS -X POST "$BASE/api/jobs/$JOB/result" -H "Authorization: Bearer $SERVICE" \
  -F "result=@$WORK/result.json;type=application/json" > /dev/null

python - "$BASE" "$SPECIALIST" "$JOB" <<'PY'
import json, sys, time, urllib.request

base, token, job = sys.argv[1], sys.argv[2], sys.argv[3]

def cards():
    request = urllib.request.Request(f"{base}/api/jobs/{job}/decisions", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request) as response:
        return json.load(response)

before = cards()
print(f"до ожидания: карточек {len(before)}, значение первой «{before[0]['decision']}», роль «{before[0]['role']}»")
time.sleep(12)
after = cards()
print("после ожидания:")
for card in after:
    print(f"  {card['reason_code']}: {card['decision']} / роль {card['role']} / кто {card.get('decided_by')}")

timed_out = [c for c in after if c['decision'] == 'timeout']
escalated = [c for c in after if c['decision'] == 'pending' and c['role'] == 'department_head']
print("итог:", "таймаут зафиксирован, карточка передана руководителю, автоматического согласия нет"
      if timed_out and escalated else "ОШИБКА: ожидались таймаут и карточка руководителя")
sys.exit(0 if timed_out and escalated else 1)
PY
