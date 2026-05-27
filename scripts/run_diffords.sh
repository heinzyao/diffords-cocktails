#!/bin/bash

# Difford's Guide Entry Script
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
LOG_FILE="$LOG_DIR/diffords_${TIMESTAMP}.log"

# Change to project directory
cd "$PROJECT_DIR"

echo "==========================================" | tee -a "$LOG_FILE"
echo "Difford's Guide Cocktails - Scheduled Run" | tee -a "$LOG_FILE"
echo "Start Time: $(date)" | tee -a "$LOG_FILE"
echo "==========================================" | tee -a "$LOG_FILE"


# Check if run_diffords.py exists
if [ ! -f "$PROJECT_DIR/run_diffords.py" ]; then
    echo "❌ Error: run_diffords.py not found at $PROJECT_DIR/run_diffords.py" | tee -a "$LOG_FILE"
    exit 1
fi

# Run the diffords scraper with incremental mode
echo "Starting diffords scraper in INCREMENTAL mode..." | tee -a "$LOG_FILE"
uv run python run_diffords.py --mode incremental --db-path diffords.db --notify-line 2>&1 | tee -a "$LOG_FILE"
EXIT_CODE=${PIPESTATUS[0]}
if [ "$EXIT_CODE" -eq 0 ]; then
    echo "" | tee -a "$LOG_FILE"
    echo "✅ Difford's scraper completed successfully!" | tee -a "$LOG_FILE"
    echo "End Time: $(date)" | tee -a "$LOG_FILE"
    echo "Log saved to: $LOG_FILE" | tee -a "$LOG_FILE"
    
    # Send success notification (macOS)
    osascript -e 'display notification "Difford'\''s Guide scraper completed successfully!" with title "Diffords Success"' 2>/dev/null || true
    
    # Optional: Send email notification (requires mail setup)
    # echo "Diffords scraper completed successfully at $(date)" | mail -s "Diffords - Success" your-email@example.com
    
else
    echo "" | tee -a "$LOG_FILE"
    echo "❌ Diffords scraper failed with exit code $EXIT_CODE" | tee -a "$LOG_FILE"
    echo "End Time: $(date)" | tee -a "$LOG_FILE"
    echo "Log saved to: $LOG_FILE" | tee -a "$LOG_FILE"
    
    # Send failure notification (macOS)
    osascript -e 'display notification "Difford'\''s Guide scraper failed! Check logs." with title "Diffords Error"' 2>/dev/null || true
    
    # Optional: Send email notification on failure
    # echo "Diffords scraper failed at $(date). Check logs at $LOG_FILE" | mail -s "Diffords - FAILED" your-email@example.com
    
    exit 1
fi

echo "==========================================" | tee -a "$LOG_FILE"
