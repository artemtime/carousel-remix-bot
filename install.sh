#!/usr/bin/env bash
# Установка бота каруселей в ваш Google Cloud. Запускать в Cloud Shell (терминал Google в браузере):
#   git clone https://github.com/artemtime/carousel-remix-bot.git carousel-remix && cd carousel-remix && bash install.sh
# Повторный запуск = обновление (ключи можно оставить прежними, нажав Enter).
# Со своего компьютера без вопросов: ключи берутся из переменных GEMINI_API_KEY, APIFY_TOKEN,
# TELEGRAM_BOT_TOKEN (например, из .env: set -a; . ./.env; set +a; bash install.sh).
set -euo pipefail

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ask_secret() {  # ask_secret ИМЯ "подсказка" [ПЕРЕМЕННАЯ] — спрашивает, если секрета ещё нет или хотят заменить
  local name="$1" hint="$2" value="${3:+${!3:-}}"
  if [ -n "$value" ]; then  # ключ уже передан переменной — вопросов не задаём
    if gcloud secrets describe "$name" --project "$PROJECT" >/dev/null 2>&1; then
      [ "$(gcloud secrets versions access latest --secret "$name" --project "$PROJECT" 2>/dev/null)" = "$value" ] && return 0
      printf '%s' "$value" | gcloud secrets versions add "$name" --project "$PROJECT" --data-file=- >/dev/null 2>&1
    else
      printf '%s' "$value" | gcloud secrets create "$name" --project "$PROJECT" --data-file=- >/dev/null 2>&1
    fi
    return 0
  fi
  if gcloud secrets describe "$name" --project "$PROJECT" >/dev/null 2>&1; then
    read -r -s -p "$hint (Enter — оставить прежний): " value; echo
    [ -z "$value" ] && return 0
    printf '%s' "$value" | gcloud secrets versions add "$name" --project "$PROJECT" --data-file=- >/dev/null 2>&1
  else
    while [ -z "${value:-}" ]; do read -r -s -p "$hint: " value; echo; done
    printf '%s' "$value" | gcloud secrets create "$name" --project "$PROJECT" --data-file=- >/dev/null 2>&1
  fi
}
random_secret() {
  local name="$1"
  gcloud secrets describe "$name" --project "$PROJECT" >/dev/null 2>&1 && return 0
  head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40 |
    gcloud secrets create "$name" --project "$PROJECT" --data-file=- >/dev/null 2>&1
}

fail() { printf '\n\033[1;31m%s\033[0m\n' "$*"; exit 1; }
LOG="${TMPDIR:-/tmp}/carousel-install.log"; : > "$LOG"
# Служебный английский вывод Google — в журнал; на экран только при ошибке.
quiet() { "$@" >>"$LOG" 2>&1 || { echo; tail -15 "$LOG"; fail "Шаг не удался — выше ответ Google (по-английски). Пришлите его в чат курса."; }; }
# Проверка ключей: сразу и по-русски, а не загадочной ошибкой на первой карусели.
check_keys() {
  local g a t
  g=$(gcloud secrets versions access latest --secret carousel-gemini --project "$PROJECT")
  a=$(gcloud secrets versions access latest --secret carousel-apify --project "$PROJECT")
  t=$(gcloud secrets versions access latest --secret carousel-telegram --project "$PROJECT")
  [ "$(curl -s --max-time 20 -o /dev/null -w '%{http_code}' -H "x-goog-api-key: $g" \
      https://generativelanguage.googleapis.com/v1beta/models)" = "200" ] || \
    fail "Ключ Google AI Studio не подходит. Скопируйте его заново (aistudio.google.com/apikey), запустите bash install.sh ещё раз и вставьте новый."
  [ "$(curl -s --max-time 20 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $a" https://api.apify.com/v2/users/me)" = "200" ] || \
    fail "Ключ Apify не подходит. Скопируйте Personal API token заново (console.apify.com → Settings → API & Integrations), запустите bash install.sh ещё раз и вставьте его."
  curl -s "https://api.telegram.org/bot${t}/getMe" | grep -q '"ok":true' || \
    fail "Токен бота не подходит. В @BotFather: /mybots → ваш бот → API Token, скопируйте его, запустите bash install.sh ещё раз и вставьте."
  echo "Ключи проверены: Google, Apify и Telegram их приняли."
}

choose_project() {  # список проектов по номеру; неверный номер — спрашиваем снова
  mapfile -t IDS < <(gcloud projects list --format='value(projectId)')
  [ "${#IDS[@]}" -gt 0 ] || fail "В аккаунте нет ни одного проекта Google Cloud. Сделайте шаг 2 инструкции (Create project в AI Studio)."
  echo "Ваши проекты Google Cloud:"
  for k in "${!IDS[@]}"; do
    echo "  $((k + 1)). $(gcloud projects describe "${IDS[$k]}" --format='value(name)') (${IDS[$k]})"
  done
  while true; do
    read -r -p "Номер проекта, где вы делали ключ AI Studio: " N || fail "Не выбран проект."
    if [[ "$N" =~ ^[0-9]+$ ]] && [ "$N" -ge 1 ] && [ "$N" -le "${#IDS[@]}" ]; then
      PROJECT="${IDS[$((N - 1))]}"; return
    fi
    echo "Нужен номер от 1 до ${#IDS[@]} — просто цифра."
  done
}
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
if [ -z "$PROJECT" ]; then
  choose_project
elif [ -t 0 ]; then  # проект выбран вверху консоли — называем его и даём сменить
  echo "Ставлю бота в проект: $(gcloud projects describe "$PROJECT" --format='value(name)' 2>/dev/null) ($PROJECT)"
  read -r -p "Верно? Enter — да, n — выбрать другой: " OK || true
  [[ "${OK:-}" =~ ^[nNнН] ]] && choose_project
fi
echo "Проект: $PROJECT"
REGION="${REGION:-europe-west1}"
SERVICE="carousel-bot"
BUCKET="${PROJECT}-carousel-data"
QUEUE="carousel-jobs"
gcloud config set project "$PROJECT" >/dev/null

say "1/8 Включаю нужные службы Google (1–2 минуты)…"
quiet gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  cloudtasks.googleapis.com secretmanager.googleapis.com storage.googleapis.com cloudscheduler.googleapis.com

say "2/8 Ключи. Их видите только вы, в коде их нет."
ask_secret carousel-gemini   "Ключ Google AI Studio (aistudio.google.com/apikey)" GEMINI_API_KEY
ask_secret carousel-apify    "Ключ Apify (console.apify.com → Settings → API & Integrations)" APIFY_TOKEN
ask_secret carousel-telegram "Токен бота от @BotFather" TELEGRAM_BOT_TOKEN
random_secret carousel-webhook
random_secret carousel-work
check_keys

say "3/8 Хранилище для героев, стилей и готовых каруселей…"
gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 || \
  gcloud storage buckets create "gs://$BUCKET" --location "$REGION" --uniform-bucket-level-access >/dev/null 2>&1
if ! gcloud storage ls "gs://$BUCKET/settings.json" >/dev/null 2>&1; then
  gcloud storage cp workspace/settings.example.json "gs://$BUCKET/settings.json" >/dev/null 2>&1
fi

say "4/8 Очередь задач (чтобы Telegram не ждал, пока рисуются картинки)…"
gcloud tasks queues describe "$QUEUE" --location "$REGION" >/dev/null 2>&1 || \
  gcloud tasks queues create "$QUEUE" --location "$REGION" --max-attempts 2 --max-concurrent-dispatches 2 >/dev/null 2>&1

say "5/8 Права для бота…"
NUMBER=$(gcloud projects describe "$PROJECT" --format 'value(projectNumber)')
SA="${NUMBER}-compute@developer.gserviceaccount.com"
for role in roles/secretmanager.secretAccessor roles/cloudtasks.enqueuer roles/storage.objectAdmin; do
  quiet gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role "$role" \
    --condition None
done

say "6/8 Собираю и запускаю бота (3–5 минут)…"
# Одна копия бота: бот личный, а «занят ли он» и шаг человека должны быть одни на всех
COMMON=(--region "$REGION" --allow-unauthenticated --timeout 1800 --memory 1Gi --cpu 1 --max-instances 1
  --set-secrets "GEMINI_API_KEY=carousel-gemini:latest,APIFY_TOKEN=carousel-apify:latest,TELEGRAM_BOT_TOKEN=carousel-telegram:latest,TELEGRAM_WEBHOOK_SECRET=carousel-webhook:latest,WORK_SECRET=carousel-work:latest"
  --add-volume "name=data,type=cloud-storage,bucket=$BUCKET"
  --add-volume-mount "volume=data,mount-path=/app/workspace")
quiet gcloud run deploy "$SERVICE" --source . "${COMMON[@]}" --quiet
URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')
quiet gcloud run services update "$SERVICE" --region "$REGION" \
  --update-env-vars "SERVICE_URL=$URL,TASKS_QUEUE=projects/$PROJECT/locations/$REGION/queues/$QUEUE" --quiet

say "7/8 Подключаю бота к Telegram…"
TOKEN=$(gcloud secrets versions access latest --secret carousel-telegram)
HOOK=$(gcloud secrets versions access latest --secret carousel-webhook)
curl -s "https://api.telegram.org/bot${TOKEN}/setWebhook" \
  -d "url=${URL}/telegram" -d "secret_token=${HOOK}" -d 'allowed_updates=["message","callback_query"]' >/dev/null
BOT=$(curl -s "https://api.telegram.org/bot${TOKEN}/getMe" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("result",{}).get("username","ваш_бот"))')
STATUS=$(curl -s -o /dev/null -w '%{http_code}' "$URL/health")

say "8/8 Разведка по расписанию (день, час и часовой пояс выбираются в боте)…"
# Облако будит бота каждый час, а бот сам решает, пора ли: «🔎 Разведка» → «🕘 Когда проверять».
WORK=$(gcloud secrets versions access latest --secret carousel-work)
JOB=(--location "$REGION" --schedule "7 * * * *" --time-zone "Etc/UTC" --uri "$URL/work" --http-method POST
  --attempt-deadline 1800s --message-body '{"_cron":"tick"}')
if gcloud scheduler jobs describe carousel-weekly --location "$REGION" >/dev/null 2>&1; then
  quiet gcloud scheduler jobs update http carousel-weekly "${JOB[@]}" \
    --update-headers "Content-Type=application/json,X-Work-Secret=$WORK"
else
  quiet gcloud scheduler jobs create http carousel-weekly "${JOB[@]}" \
    --headers "Content-Type=application/json,X-Work-Secret=$WORK"
fi
NEW_OWNER=1
gcloud storage ls "gs://$BUCKET/.bot_owner" >/dev/null 2>&1 && NEW_OWNER=0

if [ "$STATUS" = "200" ]; then
  say "Готово! Откройте в Telegram @$BOT и нажмите /start."
  [ "$NEW_OWNER" = "1" ] && echo "Первый, кто нажмёт /start, станет хозяином бота — остальным он отвечать не будет. Нажмите сами."
  echo "Когда приходит разведка — выберите в боте: «🔎 Разведка» → «🕘 Когда проверять»."
  echo "Эту вкладку можно закрыть — бот работает в облаке."
else
  say "Бот запущен, но проверка ответила $STATUS. Посмотрите журнал: gcloud run services logs read $SERVICE --region $REGION"
fi
