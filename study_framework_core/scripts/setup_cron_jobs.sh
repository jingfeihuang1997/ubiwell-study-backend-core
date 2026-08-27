#!/bin/bash

# Study Framework - Cron Job Setup Script
# This script sets up automated cron jobs for data processing

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STUDY_DIR="$(dirname "$SCRIPT_DIR")"

# Function to show usage
show_usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --user SYSTEM_USER    System user to run cron jobs (default: current user)"
    echo "  --env ENV_NAME        Conda environment name (default: study-env)"
    echo "  --remove              Remove existing cron jobs instead of adding them"
    echo "  --help                Show this help message"
    echo ""
    echo "This script sets up the following cron jobs:"
    echo "  - Process phone and Garmin data every 2 hours (at minute 0)"
    echo "  - Generate summaries and plots every 2 hours (at minute 30)"
    echo "  - Ensures fresh data is processed before summarization"
    echo ""
    echo "Examples:"
    echo "  $0 --user studyuser --env my-study-env"
    echo "  $0 --remove"
}

# Default values
SYSTEM_USER=$(whoami)
ENV_NAME="study-env"
REMOVE_JOBS=false

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --user)
            SYSTEM_USER="$2"
            shift 2
            ;;
        --env)
            ENV_NAME="$2"
            shift 2
            ;;
        --remove)
            REMOVE_JOBS=true
            shift
            ;;
        --help)
            show_usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            show_usage
            exit 1
            ;;
    esac
done

# Check if running as root or the specified user
if [[ "$(whoami)" != "$SYSTEM_USER" && "$(whoami)" != "root" ]]; then
    echo "Error: This script must be run as root or the specified user ($SYSTEM_USER)"
    exit 1
fi

# Get the full path to the scripts
PROCESS_SCRIPT="$STUDY_DIR/scripts/process_data.sh"
SUMMARY_SCRIPT="$STUDY_DIR/scripts/generate_summaries.sh"

# Check if scripts exist
if [[ ! -f "$PROCESS_SCRIPT" ]]; then
    echo "Error: Process script not found: $PROCESS_SCRIPT"
    exit 1
fi

if [[ ! -f "$SUMMARY_SCRIPT" ]]; then
    echo "Error: Summary script not found: $SUMMARY_SCRIPT"
    exit 1
fi

# Make scripts executable
chmod +x "$PROCESS_SCRIPT"
chmod +x "$SUMMARY_SCRIPT"

echo "Setting up cron jobs for Study Framework..."
echo "System user: $SYSTEM_USER"
echo "Conda environment: $ENV_NAME"
echo "Study directory: $STUDY_DIR"
echo ""

if [[ "$REMOVE_JOBS" == true ]]; then
    echo "Removing existing cron jobs..."
    
    # Remove existing cron jobs
    (crontab -u "$SYSTEM_USER" -l 2>/dev/null | grep -v "process_data.sh" | grep -v "generate_summaries.sh") | crontab -u "$SYSTEM_USER" -
    
    echo "✅ Existing cron jobs removed!"
else
    echo "Adding new cron jobs..."
    
    # Create temporary file for new crontab
    TEMP_CRON=$(mktemp)
    
    # Get existing crontab
    crontab -u "$SYSTEM_USER" -l 2>/dev/null > "$TEMP_CRON"
    
    # Add new cron jobs
    cat >> "$TEMP_CRON" << EOF

# Study Framework - Data Processing Jobs
# Process phone and Garmin data every 2 hours (at minute 0)
0 */2 * * * $PROCESS_SCRIPT --action process_data --env $ENV_NAME >> $STUDY_DIR/logs/cron_process_data.log 2>&1
0 */2 * * * $PROCESS_SCRIPT --action process_garmin --env $ENV_NAME >> $STUDY_DIR/logs/cron_garmin.log 2>&1

# Generate summaries and plots 30 minutes after processing (at minute 30)
30 */2 * * * $SUMMARY_SCRIPT --env $ENV_NAME >> $STUDY_DIR/logs/cron_summaries.log 2>&1
30 */2 * * * $SUMMARY_SCRIPT --plots --env $ENV_NAME >> $STUDY_DIR/logs/cron_plots.log 2>&1

# Rotate cron logs every 6 hours
15 */6 * * * /usr/sbin/logrotate -s $STUDY_DIR/logs/logrotate.status $STUDY_DIR/logs/logrotate.conf
EOF
    
    # Install new crontab
    crontab -u "$SYSTEM_USER" "$TEMP_CRON"
    
    # Clean up temporary file
    rm "$TEMP_CRON"
    
    # Create log directories
    mkdir -p "$STUDY_DIR/logs"

    LOGROTATE_CONF="$STUDY_DIR/logs/logrotate.conf"
    cat > "$LOGROTATE_CONF" << LOGROTATE_EOF
$STUDY_DIR/logs/cron_process_data.log
$STUDY_DIR/logs/cron_garmin.log
$STUDY_DIR/logs/cron_summaries.log
$STUDY_DIR/logs/cron_plots.log
{
    rotate 7
    size 50M
    compress
    missingok
    notifempty
    copytruncate
}
LOGROTATE_EOF
    
    echo "✅ Cron jobs added successfully!"
    echo ""
    echo "📋 Installed cron jobs:"
    echo "  - Process phone data: Every 2 hours (at minute 0)"
    echo "  - Process Garmin files: Every 2 hours (at minute 0)"
    echo "  - Generate summaries: Every 2 hours (at minute 30)"
    echo "  - Generate plots: Every 2 hours (at minute 30)"
    echo ""
    echo "📁 Log files:"
    echo "  - $STUDY_DIR/logs/cron_process_data.log"
    echo "  - $STUDY_DIR/logs/cron_summaries.log"
    echo "  - $STUDY_DIR/logs/cron_garmin.log"
    echo "  - $STUDY_DIR/logs/cron_plots.log"
    echo ""
    echo "🔧 To view cron jobs: crontab -u $SYSTEM_USER -l"
    echo "🔧 To edit cron jobs: crontab -u $SYSTEM_USER -e"
fi
