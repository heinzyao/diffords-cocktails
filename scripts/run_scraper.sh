#!/bin/bash

# Distiller Scraper Entry Script
# Usage: manual run or Cloud Run container entrypoint
# Formerly scheduled via local launchd (removed — now Cloud Run only)

set -eo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# 載入環境變數（LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID 等）
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi
LOG_DIR="$PROJECT_DIR/logs"
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')

# Create log directory if not exists
mkdir -p "$LOG_DIR"

# Log file paths
LOG_FILE="$LOG_DIR/scraper_${TIMESTAMP}.log"

# Change to project directory
cd "$PROJECT_DIR"

echo "==========================================" | tee -a "$LOG_FILE"
echo "Distiller Scraper - Scheduled Run" | tee -a "$LOG_FILE"
echo "Start Time: $(date)" | tee -a "$LOG_FILE"
echo "==========================================" | tee -a "$LOG_FILE"


# Check if run.py exists
if [ ! -f "$PROJECT_DIR/run.py" ]; then
    echo "❌ Error: run.py not found at $PROJECT_DIR/run.py" | tee -a "$LOG_FILE"
    exit 1
fi

# Run the scraper with full mode
echo "Starting scraper in FULL mode..." | tee -a "$LOG_FILE"
uv run python run.py --mode full --output both --use-api --notify-line 2>&1 | tee -a "$LOG_FILE"
EXIT_CODE=${PIPESTATUS[0]}
if [ "$EXIT_CODE" -eq 0 ]; then
    echo "" | tee -a "$LOG_FILE"
    echo "✅ Scraper completed successfully!" | tee -a "$LOG_FILE"
    echo "End Time: $(date)" | tee -a "$LOG_FILE"
    echo "Log saved to: $LOG_FILE" | tee -a "$LOG_FILE"
    
    # Send success notification (macOS)
    osascript -e 'display notification "Distiller scraper completed successfully!" with title "Scraper Success"' 2>/dev/null || true
    
    # Optional: Send email notification (requires mail setup)
    # echo "Scraper completed successfully at $(date)" | mail -s "Distiller Scraper - Success" your-email@example.com
    
else
    echo "" | tee -a "$LOG_FILE"
    echo "❌ Scraper failed with exit code $EXIT_CODE" | tee -a "$LOG_FILE"
    echo "End Time: $(date)" | tee -a "$LOG_FILE"
    echo "Log saved to: $LOG_FILE" | tee -a "$LOG_FILE"
    
    # Send failure notification (macOS)
    osascript -e 'display notification "Distiller scraper failed! Check logs." with title "Scraper Error"' 2>/dev/null || true
    
    # Optional: Send email notification on failure
    # echo "Scraper failed at $(date). Check logs at $LOG_FILE" | mail -s "Distiller Scraper - FAILED" your-email@example.com
    
    exit 1
fi

echo "==========================================" | tee -a "$LOG_FILE"
