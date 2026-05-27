#!/bin/bash

set -eo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-${GCLOUD_PROJECT:-}}"
REGION="${CLOUD_RUN_REGION:-asia-east1}"
BUCKET_NAME="${GCS_BUCKET:-diffords-cocktails-data}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "GOOGLE_CLOUD_PROJECT or GCLOUD_PROJECT is required"
  exit 1
fi

gcloud config set project "$PROJECT_ID"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com storage.googleapis.com

gcloud storage buckets describe "gs://${BUCKET_NAME}" >/dev/null 2>&1 \
  || gcloud storage buckets create "gs://${BUCKET_NAME}" --location="$REGION"

docker build -f Dockerfile.diffords -t "gcr.io/${PROJECT_ID}/diffords-cocktails-scraper:latest" .
docker push "gcr.io/${PROJECT_ID}/diffords-cocktails-scraper:latest"

docker build -f Dockerfile.bot -t "gcr.io/${PROJECT_ID}/diffords-cocktails-bot:latest" .
docker push "gcr.io/${PROJECT_ID}/diffords-cocktails-bot:latest"

gcloud run jobs update diffords-cocktails-scraper \
  --image "gcr.io/${PROJECT_ID}/diffords-cocktails-scraper:latest" \
  --region "$REGION" \
  --set-env-vars "GCS_BUCKET=${BUCKET_NAME},GCS_DB_BLOB=diffords.db" \
  --set-secrets "LINE_CHANNEL_ID=LINE_CHANNEL_ID:latest,LINE_CHANNEL_SECRET=LINE_CHANNEL_SECRET:latest,LINE_USER_ID=LINE_USER_ID:latest" \
  || gcloud run jobs create diffords-cocktails-scraper \
    --image "gcr.io/${PROJECT_ID}/diffords-cocktails-scraper:latest" \
    --region "$REGION" \
    --memory 512Mi \
    --cpu 1 \
    --task-timeout 7200 \
    --set-env-vars "GCS_BUCKET=${BUCKET_NAME},GCS_DB_BLOB=diffords.db" \
    --set-secrets "LINE_CHANNEL_ID=LINE_CHANNEL_ID:latest,LINE_CHANNEL_SECRET=LINE_CHANNEL_SECRET:latest,LINE_USER_ID=LINE_USER_ID:latest"

gcloud run deploy diffords-cocktails-bot \
  --image "gcr.io/${PROJECT_ID}/diffords-cocktails-bot:latest" \
  --region "$REGION" \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 2 \
  --allow-unauthenticated \
  --set-env-vars "GCS_BUCKET=${BUCKET_NAME},GCS_DB_BLOB=diffords.db,DIFFORDS_JOB_NAME=diffords-cocktails-scraper" \
  --set-secrets "LINE_CHANNEL_ID=LINE_CHANNEL_ID:latest,LINE_CHANNEL_SECRET=LINE_CHANNEL_SECRET:latest,LINE_USER_ID=LINE_USER_ID:latest"

echo "Deployment complete."
