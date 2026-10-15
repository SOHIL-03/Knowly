#!/usr/bin/env bash
# One-time Google Cloud setup and deploy for Knowly (Architecture v3 sections 2 and 2.5).
# Written without access to gcloud: it has been syntax-checked, not run. Review each step first.
# Usage: PROJECT_ID=my-proj REGION=us-central1 ./deploy/setup-gcp.sh
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
BUCKET="${BUCKET:-${PROJECT_ID}-knowly}"
API=knowly-api
JOB=knowly-briefing
RUN_SA="knowly-run@${PROJECT_ID}.iam.gserviceaccount.com"        # runs the API service and the job
INVOKER_SA="knowly-invoker@${PROJECT_ID}.iam.gserviceaccount.com" # signs Pub/Sub, Tasks and Scheduler calls
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
PUBSUB_AGENT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"

echo "== APIs"
gcloud services enable run.googleapis.com firestore.googleapis.com storage.googleapis.com \
  pubsub.googleapis.com cloudtasks.googleapis.com cloudscheduler.googleapis.com \
  secretmanager.googleapis.com generativelanguage.googleapis.com \
  gmail.googleapis.com firebase.googleapis.com fcm.googleapis.com logging.googleapis.com \
  clouderrorreporting.googleapis.com iamcredentials.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com

echo "== Service accounts (least privilege)"
gcloud iam service-accounts create knowly-run --display-name="Knowly API and job" || true
gcloud iam service-accounts create knowly-invoker --display-name="Knowly internal invoker" || true
for role in roles/datastore.user roles/pubsub.publisher roles/cloudtasks.enqueuer \
            roles/secretmanager.admin \
            roles/firebasemessaging.admin roles/firebaseauth.admin roles/logging.logWriter \
            roles/errorreporting.writer roles/run.developer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$RUN_SA" --role="$role" --condition=None >/dev/null
done
# secretmanager.admin: the API creates one secret per user for Gmail refresh tokens and deletes it on
# account deletion. Narrow with an IAM condition on the secret name prefix "gmail-token-" if required.
# run.developer: the API starts the Cloud Run Job for each briefing.

# Signing GCS URLs on Cloud Run (IAM signBlob) and attaching the invoker identity to Cloud Tasks.
gcloud iam service-accounts add-iam-policy-binding "$RUN_SA" --member="serviceAccount:$RUN_SA" --role=roles/iam.serviceAccountTokenCreator
gcloud iam service-accounts add-iam-policy-binding "$INVOKER_SA" --member="serviceAccount:$RUN_SA" --role=roles/iam.serviceAccountUser
# Pub/Sub creates OIDC tokens for push subscriptions.
gcloud iam service-accounts add-iam-policy-binding "$INVOKER_SA" --member="serviceAccount:$PUBSUB_AGENT" --role=roles/iam.serviceAccountTokenCreator

echo "== Firestore (native mode), security rules and indexes"
gcloud firestore databases create --location="$REGION" --type=firestore-native || true
npx --prefix frontend firebase-tools deploy --only firestore:rules,firestore:indexes,storage --project "$PROJECT_ID"

echo "== Cloud Storage (raw newsletter HTML is deleted after 7 days)"
gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access || true
gcloud storage buckets update "gs://$BUCKET" --lifecycle-file=deploy/storage-lifecycle.json
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$RUN_SA" --role=roles/storage.objectAdmin

echo "== Secrets (values are added by hand, never stored in this repository)"
# GEMINI_KEY_COUNT = how many Gemini API keys you have (1-10). Each becomes GEMINI_API_KEY_<n>,
# tried in order 1 -> n on every request (failover on exhausted quota only).
GEMINI_KEY_COUNT="${GEMINI_KEY_COUNT:-1}"
for i in $(seq 1 "$GEMINI_KEY_COUNT"); do
  gcloud secrets create "gemini-api-key-$i" --replication-policy=automatic || true
  gcloud secrets add-iam-policy-binding "gemini-api-key-$i" --member="serviceAccount:$RUN_SA" --role=roles/secretmanager.secretAccessor
done
gcloud secrets create openai-api-key --replication-policy=automatic || true
gcloud secrets add-iam-policy-binding openai-api-key --member="serviceAccount:$RUN_SA" --role=roles/secretmanager.secretAccessor
gcloud secrets create gmail-oauth-client-secret --replication-policy=automatic || true
echo "Add the OpenAI key (all text generation): printf %s 'KEY' | gcloud secrets versions add openai-api-key --data-file=-"
echo "Add each Gemini key (embeddings and TTS) with: printf %s 'KEY' | gcloud secrets versions add gemini-api-key-<n> --data-file=-   (n = 1..$GEMINI_KEY_COUNT)"
echo "and the Gmail client secret: printf %s 'CLIENT_SECRET' | gcloud secrets versions add gmail-oauth-client-secret --data-file=-"

echo "== Build the image"
gcloud artifacts repositories create knowly --repository-format=docker --location="$REGION" || true
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/knowly/api:latest"
gcloud builds submit backend --tag "$IMAGE"

COMMON_ENV="ENV=cloud,GCP_PROJECT=$PROJECT_ID,GCP_REGION=$REGION,STORAGE_BUCKET=$BUCKET,DISPATCH_MODE=cloud,GMAIL_OAUTH_CLIENT_SECRET_NAME=gmail-oauth-client-secret"
# Text generation uses OPENAI_API_KEY; embeddings and TTS use the Gemini API keys. All are read from
# Secret Manager at runtime.
SECRETS="OPENAI_API_KEY=openai-api-key:latest"
for i in $(seq 1 "$GEMINI_KEY_COUNT"); do
  SECRETS="${SECRETS:+$SECRETS,}GEMINI_API_KEY_$i=gemini-api-key-$i:latest"
done

echo "== Deploy the API service (first pass, to learn its URL)"
gcloud run deploy "$API" --image "$IMAGE" --region "$REGION" --service-account "$RUN_SA" \
  --allow-unauthenticated --min-instances=1 --timeout=900 --memory=1Gi \
  --set-env-vars "$COMMON_ENV,INTERNAL_INVOKER_SA=$INVOKER_SA,INTERNAL_BASE_URL=https://placeholder,BRIEFING_JOB_NAME=$JOB" \
  --set-secrets "$SECRETS"
API_URL="$(gcloud run services describe "$API" --region "$REGION" --format='value(status.url)')"
gcloud run services update "$API" --region "$REGION" \
  --update-env-vars "INTERNAL_BASE_URL=$API_URL,PUBLIC_API_BASE_URL=$API_URL"
# CORS: the web app is served from Firebase Hosting on the same origin (/api/** rewrite), so no
# cross-origin access is needed. Add origins with: --update-env-vars '^|^CORS_ORIGINS=["https://..."]'
# --allow-unauthenticated is needed for browser calls; every route verifies a Firebase ID token and
# /internal/* verifies a Google OIDC token signed for $INVOKER_SA.
gcloud run services add-iam-policy-binding "$API" --region "$REGION" --member="serviceAccount:$INVOKER_SA" --role=roles/run.invoker

echo "== Cloud Run Job (pipeline worker for B1-B10)"
gcloud run jobs deploy "$JOB" --image "$IMAGE" --region "$REGION" --service-account "$RUN_SA" \
  --command python --args=-m,app.job --max-retries=2 --task-timeout=1800 \
  --set-env-vars "$COMMON_ENV,INTERNAL_BASE_URL=$API_URL,INTERNAL_INVOKER_SA=$INVOKER_SA" \
  --set-secrets "$SECRETS"

echo "== Pub/Sub (push with OIDC; dead-letter after 5 failed deliveries)"
gcloud pubsub topics create run-briefing || true
gcloud pubsub topics create gmail-events || true
gcloud pubsub topics create knowly-dead-letter || true
gcloud pubsub subscriptions create knowly-dead-letter-sub --topic knowly-dead-letter || true
gcloud pubsub topics add-iam-policy-binding knowly-dead-letter --member="serviceAccount:$PUBSUB_AGENT" --role=roles/pubsub.publisher
for pair in "run-briefing:/internal/pubsub/run-briefing" "gmail-events:/internal/pubsub/gmail"; do
  topic="${pair%%:*}"
  path="${pair#*:}"
  gcloud pubsub subscriptions create "$topic-push" --topic "$topic" --push-endpoint="$API_URL$path" \
    --push-auth-service-account="$INVOKER_SA" --push-auth-token-audience="$API_URL" \
    --ack-deadline=600 --dead-letter-topic=knowly-dead-letter --max-delivery-attempts=5 \
    --min-retry-delay=10s --max-retry-delay=600s || true
  gcloud pubsub subscriptions add-iam-policy-binding "$topic-push" --member="serviceAccount:$PUBSUB_AGENT" --role=roles/pubsub.subscriber
done
# Gmail users.watch publishes through this Google-owned account.
gcloud pubsub topics add-iam-policy-binding gmail-events --member=serviceAccount:gmail-api-push@system.gserviceaccount.com --role=roles/pubsub.publisher
gcloud run services update "$API" --region "$REGION" --update-env-vars "GMAIL_PUBSUB_TOPIC_FULL=projects/$PROJECT_ID/topics/gmail-events"

echo "== Cloud Tasks (per-segment audio work: rate limited, retried with backoff)"
gcloud tasks queues create segment-audio --location="$REGION" \
  --max-dispatches-per-second=2 --max-concurrent-dispatches=10 --max-attempts=3 --min-backoff=30s || true

echo "== Cloud Scheduler"
gcloud scheduler jobs create http knowly-dispatch --location="$REGION" --schedule="*/5 * * * *" \
  --uri="$API_URL/internal/scheduler/dispatch" --http-method=POST \
  --oidc-service-account-email="$INVOKER_SA" --oidc-token-audience="$API_URL" || true
gcloud scheduler jobs create http knowly-renew-watches --location="$REGION" --schedule="0 3 * * *" \
  --uri="$API_URL/internal/scheduler/renew-watches" --http-method=POST \
  --oidc-service-account-email="$INVOKER_SA" --oidc-token-audience="$API_URL" || true

echo "== Web app on Firebase Hosting (/api/** is rewritten to the Cloud Run service, see firebase.json)"
echo "Set the VITE_* values in frontend/.env.production first (see frontend/.env.example)."
npm --prefix frontend ci
npm --prefix frontend run build
npx --prefix frontend firebase-tools deploy --only hosting --project "$PROJECT_ID"

echo "Done. Check readiness: curl $API_URL/api/ready"
