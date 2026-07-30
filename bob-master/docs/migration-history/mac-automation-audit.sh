#!/bin/bash
# Advanced Marketers — Mac Automation Audit
# Run on the OLD MacBook Pro:  bash ~/Downloads/mac-automation-audit.sh
# Writes a report to your Desktop: mac-automation-audit.txt

OUT="$HOME/Desktop/mac-automation-audit.txt"
exec > "$OUT" 2>&1

echo "MAC AUTOMATION AUDIT — $(hostname) — $(date)"
echo "macOS $(sw_vers -productVersion)"
echo "======================================================================"

section() { echo; echo "===== $1 ====="; }

section "1. USER LAUNCH AGENTS (~/Library/LaunchAgents) — per-user scheduled jobs"
ls -la "$HOME/Library/LaunchAgents" 2>/dev/null || echo "(none)"
for f in "$HOME/Library/LaunchAgents"/*.plist; do
  [ -e "$f" ] || continue
  echo "--- $f"
  plutil -p "$f" 2>/dev/null | grep -E "Label|Program|ProgramArguments|StartInterval|StartCalendarInterval|RunAtLoad" -A 3
done

section "2. SYSTEM LAUNCH AGENTS/DAEMONS (non-Apple)"
ls /Library/LaunchAgents /Library/LaunchDaemons 2>/dev/null | grep -v "^com.apple" || echo "(none non-Apple)"

section "3. CRON JOBS"
crontab -l 2>/dev/null || echo "(no crontab)"

section "4. LOGIN ITEMS (apps that start automatically)"
osascript -e 'tell application "System Events" to get the name of every login item' 2>/dev/null || echo "(could not read — may need permission)"

section "5. macOS SHORTCUTS (automations built in Shortcuts app)"
shortcuts list 2>/dev/null || echo "(none or Shortcuts CLI unavailable)"

section "6. AUTOMATION APPS INSTALLED (/Applications)"
ls /Applications | grep -iE "keyboard maestro|hazel|n8n|zapier|make|alfred|bettertouchtool|shortcuts|automator|raycast|hammerspoon|docker|zoom|slack|claude" || echo "(none of the usual suspects)"

section "7. AUTOMATOR WORKFLOWS & FOLDER ACTIONS"
find "$HOME/Library/Workflows" "$HOME/Library/Services" -name "*.workflow" 2>/dev/null | head -50 || true
echo "(empty = none)"

section "8. HOMEBREW BACKGROUND SERVICES"
command -v brew >/dev/null && brew services list 2>/dev/null || echo "(homebrew not installed or no services)"

section "9. DOCKER CONTAINERS (n8n etc. often run here)"
command -v docker >/dev/null && docker ps -a 2>/dev/null || echo "(docker not installed/running)"

section "10. NODE/PM2 PROCESSES"
command -v pm2 >/dev/null && pm2 list 2>/dev/null || echo "(pm2 not installed)"
ps aux | grep -iE "node|python" | grep -v grep | grep -v "Claude" | awk '{print $11, $12, $13}' | sort -u | head -20

section "11. BROWSER LEFT RUNNING FOR AUTOMATIONS? (Chrome extensions dir)"
ls "$HOME/Library/Application Support/Google/Chrome/Default/Extensions" 2>/dev/null | wc -l | xargs -I{} echo "{} Chrome extensions installed (review manually in chrome://extensions)"

echo
echo "======================================================================"
echo "DONE. This file is on your Desktop: mac-automation-audit.txt"
echo "Attach it to the Claude session and Claude will map each item to the migration plan."
open -R "$OUT" 2>/dev/null
