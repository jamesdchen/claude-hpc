#!/usr/bin/env bash
# Install the wait-predictor cron entries.
#
# Two cron jobs:
#   - snapshot_squeue.py  — runs every 5 minutes; accumulates the
#                           queue-snapshot history the LightGBM-residual
#                           predictor needs (~7-14 days before useful).
#   - train_wait_predictor.py — runs daily at 03:00; refits the model
#                               from snapshots + sacct history.
#
# Usage:
#   ./install_cron.sh <ssh-target> <experiment-dir> <hpc-agent-repo-dir>
#
# Idempotent — checks for existing crontab entries and skips them.
# Requires lightgbm to be installed (hpc-agent-pro's `forecasting` extra).

set -euo pipefail

if [[ $# -ne 3 ]]; then
    cat >&2 <<EOF
Usage: $0 <ssh-target> <experiment-dir> <hpc-agent-repo-dir>

  <ssh-target>          e.g. alice@cluster.example.edu
  <experiment-dir>      absolute path to the experiment directory
  <hpc-agent-repo-dir>  absolute path to the hpc-agent checkout
                        (i.e. \$(git rev-parse --show-toplevel))

The repo dir is needed so the cron uses the framework's venv python
directly — the cron job's environment doesn't inherit the user's shell.
EOF
    exit 2
fi

SSH_TARGET="$1"
EXPERIMENT_DIR="$2"
CLAUDE_HPC_REPO="$3"

if ! python -c "import lightgbm" 2>/dev/null; then
    echo "lightgbm not installed — install hpc-agent-pro with the 'forecasting' extra:" >&2
    echo "  pip install 'hpc-agent-pro[forecasting]'" >&2
    exit 1
fi

# Snapshot cron (every 5 minutes)
SNAPSHOT_LINE="*/5 * * * * cd \"$EXPERIMENT_DIR\" && \"$CLAUDE_HPC_REPO/.venv/bin/python\" \"$CLAUDE_HPC_REPO/hpc-agent-pro/scripts/snapshot_squeue.py\" --ssh-target \"$SSH_TARGET\" --experiment-dir \"$EXPERIMENT_DIR\" >> .hpc/snapshot_squeue.log 2>&1"
if crontab -l 2>/dev/null | grep -qF "hpc-agent-pro/scripts/snapshot_squeue.py"; then
    echo "snapshot cron already installed; skipping"
else
    (crontab -l 2>/dev/null; echo "$SNAPSHOT_LINE") | crontab -
    echo "installed snapshot cron (runs every 5 minutes)"
fi

# Nightly training cron (03:00 daily)
TRAIN_LINE="0 3 * * * cd \"$EXPERIMENT_DIR\" && \"$CLAUDE_HPC_REPO/.venv/bin/python\" \"$CLAUDE_HPC_REPO/scripts/extract_sacct_history.py\" --ssh-target \"$SSH_TARGET\" --since-days 30 --out completed_jobs.json && \"$CLAUDE_HPC_REPO/.venv/bin/python\" \"$CLAUDE_HPC_REPO/hpc-agent-pro/scripts/train_wait_predictor.py\" --completed-jobs completed_jobs.json --slot-counts slot_counts.json --experiment-dir \"$EXPERIMENT_DIR\" >> .hpc/train_wait_predictor.log 2>&1"
if crontab -l 2>/dev/null | grep -qF "train_wait_predictor"; then
    echo "training cron already installed; skipping"
else
    (crontab -l 2>/dev/null; echo "$TRAIN_LINE") | crontab -
    echo "installed training cron (runs daily at 03:00)"
fi

echo ""
echo "To remove either entry: \`crontab -e\` and delete the matching line."
