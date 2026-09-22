#!/usr/bin/env bash
# Fleet Command Center — 60-second start
set -e
echo "🛰 Fleet Command Center installer"
echo "---------------------------------"
command -v python3 >/dev/null || { echo "python3 required"; exit 1; }
mkdir -p ~/.local/share/fleet-cc/logs
cp fleet_command.py fleet_command_page.html ~/.local/share/fleet-cc/ 2>/dev/null || true
cd ~/.local/share/fleet-cc
[ -f fleet_hosts.json ] || { echo '[{"hub": {"addr": "127.0.0.1", "user": "'$USER'", "method": "local", "label": "HUB — this machine", "ports": {"console": 9220}, "status": "LIVE"}}]' > /dev/null; python3 - <<'EOF'
import json, os
reg = {"hub": {"addr": "127.0.0.1", "user": os.environ.get("USER", "you"), "method": "local",
      "label": "HUB — this machine", "ports": {"console": 9220}, "status": "LIVE"}}
json.dump(reg, open("fleet_hosts.json", "w"), indent=2)
print("created fleet_hosts.json — add your machines, then restart the console")
EOF
}
nohup python3 fleet_command.py >> logs/fleet_command.log 2>&1 &
sleep 2
echo "✅ Console: http://$(hostname -I | awk '{print $1}'):9220 (add TLS certs for passkeys — see docs/SECURITY.md)"
