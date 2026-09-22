# 🛰 Fleet Command Center

**One web console to command every machine on your LAN.**

Built for the person who runs 3-15 machines at home — AI rigs, render boxes,
laptops, NUCs — and is tired of SSHing into five terminals to answer
"is everything okay?"

![status](https://img.shields.io/badge/status-battle--tested-brightgreen) ![deps](https://img.shields.io/badge/dependencies-zero-blue) ![lang](https://img.shields.io/badge/python-3.8%2B-yellow)

## What it does

```
┌─────────────────────────────────────────────────────┐
│  FLEET COMMAND CENTER          https://your-hub.local:9220 │
├──────────┬──────────┬──────────────┬─────────────────┤
│ KALI     │ ZEPHYRE  │ TEMPORALFLOW │ NUC25           │
│ ONLINE   │ ONLINE   │ ONLINE       │ PENDING         │
│ 3 chips  │ 2 chips  │ 2 chips      │ needs sshd      │
├──────────┴──────────┴──────────────┴─────────────────┤
│ THUNDER  — broadcast one command to every machine    │
│ SPECTRUM — live CPU/RAM/disk/GPU from all members    │
│ FORGE    — submit GPU render jobs to the big rig     │
│ ARMORY   — 90 security tools + app launcher          │
│ SENTINEL — Telegram alerts when anything goes down   │
└─────────────────────────────────────────────────────┘
```

- **Host cards** with live status: ping, SSH auth, per-service chips, last sync
- **THUNDER** — fire one command at every machine (per-OS command mapping,
  parallel execution, aggregated results)
- **SPECTRUM** — combined telemetry: CPU/RAM/disk per member, GPU util/VRAM/temp
  from the render rig, latency from the hub
- **FORGE** — submit jobs to your GPU box from the couch; artifacts pulled back
  to the hub with checksum verification
- **ARMORY** — drive HexStrike-AI (nmap, sqlmap, metasploit...) and launch any
  desktop app on the hub from any device
- **SENTINEL** — change-alerted monitoring with Telegram delivery
- **Wake-on-LAN**, push/pull file sync with merge policy, fire-and-forget task
  tracking, full audit log
- **Security** — TLS-only, WebAuthn/passkey face-unlock (your phone's Face ID
  signs the challenge; nothing biometric touches the server), recovery PIN,
  session tokens

## The kicker: zero dependencies

One Python file + one HTML file. Standard library only. If your box runs
Python 3, it runs this. No npm install, no docker pull, no config yaml.

```bash
git clone https://github.com/brandonrobertbrown/fleet-command-center.git
cd fleet-command-center
python3 fleet_command.py          # serves https://yourhost.local:9220
```

Edit `fleet_hosts.json`, add your machines, done. Windows members need only
OpenSSH Server (Settings → Optional Features).

## Docs
- [Setup guide](docs/SETUP.md) — 10 minutes, start to console
- [THUNDER command reference](docs/THUNDER.md)
- [Adding your own tools to ARMORY](docs/ARMORY.md)
- [Security model](docs/SECURITY.md) — how the passkey flow works

## Screenshots
See `screenshots/` — dark ops-console UI, mobile-friendly.

## License
MIT. See [LICENSE](LICENSE). If it makes your homelab life better, a ⭐ or
[sponsorship](https://github.com/sponsors/brandonrobertbrown) keeps development alive.

Want the armored version? The same author ships
[homelab & AI-ops playbooks at Alpha Desk](https://brandonrobertbrown.github.io/coach-empire-store/) —
including the [Homelab Ops Command Pack](https://coachcaptain.gumroad.com) with the
maintenance schedules and credential trackers this console pairs with.

---
*Forged in a real 24/7 AI fleet running renders, red-team tooling, and a very
suspicious cat.*
