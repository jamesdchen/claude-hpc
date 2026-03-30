#!/bin/bash
set -euo pipefail

# claude-hpc setup — install config, commands, and Python package
# Safe to re-run (idempotent). Run: bash setup.sh

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="$HOME/.claude"

# ── Prerequisites ──────────────────────────────────────────────────
echo "Checking prerequisites..."
missing=0
for cmd in python3 pip ssh rsync ruff mypy jq; do
    if command -v "$cmd" &>/dev/null; then
        echo "  ✓ $cmd"
    else
        echo "  ✗ $cmd — not found"
        missing=1
    fi
done

if [ $missing -eq 1 ]; then
    echo ""
    echo "Install missing tools before continuing:"
    echo "  pip install ruff mypy"
    echo "  (apt|brew|choco) install jq rsync openssh-client"
    exit 1
fi

# ── Helper: backup-aware copy ─────────────────────────────────────
safe_copy() {
    local src="$1" dst="$2"
    if [ -e "$dst" ] && [ ! -L "$dst" ]; then
        if ! diff -q "$src" "$dst" &>/dev/null; then
            local bak="${dst}.bak.$(date +%Y%m%d%H%M%S)"
            echo "  Backing up $dst -> $bak"
            cp -r "$dst" "$bak"
        fi
    fi
    cp -rf "$src" "$dst"
    echo "  Installed: $dst"
}

# ── Install config ────────────────────────────────────────────────
echo ""
echo "Installing Claude Code config from $REPO_DIR..."

mkdir -p "$CLAUDE_DIR"
mkdir -p "$CLAUDE_DIR/commands"

# Commands: copy each .md into ~/.claude/commands/
if ls "$REPO_DIR/commands/"*.md &>/dev/null; then
    for cmd_file in "$REPO_DIR/commands/"*.md; do
        safe_copy "$cmd_file" "$CLAUDE_DIR/commands/$(basename "$cmd_file")"
    done
else
    echo "  (no command files found in commands/)"
fi

# NOTE: We intentionally do NOT copy settings.json to ~/.claude/.
# claude-hpc is a specialization layer, not a replacement for global config.
# The slash commands (installed above) carry their own context and work
# from any project that has a project.yaml.

# ── Install Python package ────────────────────────────────────────
echo ""
echo "Installing hpc package (editable)..."
pip install -e "$REPO_DIR" --quiet
echo "  ✓ hpc package installed"

# ── Summary ───────────────────────────────────────────────────────
echo ""
echo "Setup complete."
echo ""
echo "Installed commands:"
if ls "$CLAUDE_DIR/commands/"*.md &>/dev/null; then
    for f in "$CLAUDE_DIR/commands/"*.md; do
        name=$(basename "$f" .md)
        echo "  /$name"
    done
else
    echo "  (none yet — add .md files to commands/)"
fi
echo ""
echo "Note: global ~/.claude/settings.json was NOT modified."
echo "claude-hpc layers on top of your global config via slash commands."
