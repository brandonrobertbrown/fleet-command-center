# Setup Guide (10 minutes)

## Hub (any Linux/Mac box)
1. `git clone` this repo, `cd fleet-command-center`
2. `python3 fleet_command.py` — serves on 0.0.0.0:9220 (TLS auto-on if certs exist)
3. For passkeys: create certs + hostname (see SECURITY.md), else plain HTTP works on LAN

## Members
### Windows
- Settings → System → Optional Features → Add **OpenSSH Server**
- `Start-Service sshd; Set-Service sshd -StartupType Automatic`
- Put the hub's pubkey in `C:\ProgramData\ssh\administrators_authorized_keys`
  (ACL: SYSTEM + Administrators only — `icacls` it)

### Linux/Mac
- Standard sshd + your hub key in `~/.ssh/authorized_keys`

## Registry
Edit `fleet_hosts.json`:
```json
{
  "my-rig": {
    "addr": "192.168.1.50",
    "user": "you",
    "method": "ssh",
    "remote_root": "/home/you/fleet_sync",
    "label": "MY RIG — main workstation",
    "ports": {"dashboard": 9119},
    "status": "LIVE"
  }
}
```
The hub itself gets `"method": "local"` — it probes natively, no ssh-to-self.

## Persistence (Linux hub)
```bash
crontab -e
# watchdog every 5 min:
*/5 * * * * curl -sk -m3 https://127.0.0.1:9220/api/state >/dev/null 2>&1 || (cd /path/to/repo && python3 fleet_command.py >> logs/fleet_command.log 2>&1 &)
# boot autostart:
@reboot sleep 30 && cd /path/to/repo && python3 fleet_command.py >> logs/fleet_command.log 2>&1
```
