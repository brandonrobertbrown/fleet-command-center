#!/usr/bin/env python3
"""Operator Fleet Command Center — one GUI to oversee and direct the whole fleet.

Serves from the your hub on http://your-hub.local:9220 (any LAN browser).
Stdlib only. Backend drives the real fleet: fleet_hosts.json, the sync engine,
live probes, wake-on-LAN, and the sync audit log.

  python3 fleet_command.py              # serve on 0.0.0.0:9220
  python3 fleet_command.py 9221         # custom port
"""
import io, json, base64, os, socket, struct, subprocess, threading, time, webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
HOSTS_JSON = HOME / ".hermes/fleet_hosts.json"
ENGINE = HOME / ".hermes/scripts/operator_fleet_sync.py"
AUDIT = HOME / ".hermes/logs/fleet_sync.jsonl"
IDENTITY = HOME / ".ssh/fleet_sync_ed25519"
VERSION = "1.2"
ORIGIN = "https://fleet.local:9220"
RP_ID = "fleet.local"
PAGE_FILE = HOME / ".hermes/scripts/fleet_command_page.html"
PAGE = PAGE_FILE.read_text() if PAGE_FILE.is_file() else "<h1>fleet_command_page.html missing</h1>"

# ---------------------------------------------------------------- fleet state

def load_hosts():
    try:
        return json.loads(HOSTS_JSON.read_text())
    except Exception:
        return {}

def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def audit_rows(limit=200):
    if not AUDIT.is_file():
        return []
    rows = []
    for line in AUDIT.read_text().strip().splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return list(reversed(rows))  # newest first

def audit_summary():
    """Last heartbeat result + failure streak per host, from the audit log."""
    out = {}
    for r in audit_rows(500):
        h = r.get("host")
        if not h or h in out:
            continue  # first hit per host = newest
        failed = r.get("result") == "transport_failed"
        out[h] = {
            "last_op": r.get("op"),
            "last_ts": (r.get("ts") or "")[:19],
            "last_ok": not failed,
            "last_detail": (r.get("err") or r.get("result") or "ok")[:90],
        }
    return out

# ---------------------------------------------------------------- live probes

def tcp_probe(addr, port, timeout=1.5):
    try:
        with socket.create_connection((addr, port), timeout=timeout):
            return True
    except Exception:
        return False

def ping_host(addr, timeout=1.5):
    r = subprocess.run(["ping", "-c", "1", "-W", "1", addr],
                       capture_output=True, timeout=timeout + 2)
    return r.returncode == 0

def probe_host(name, h):
    """One shot of everything we know how to check for a host."""
    st = {"name": name, "addr": h["addr"], "label": h.get("label", name),
          "user": h.get("user", ""), "remote_root": h.get("remote_root", ""),
          "pending": h.get("status", "LIVE").upper() == "PENDING"}
    local = h.get("method") == "local"  # the hub itself — probe natively, no ssh loopback
    st["ping"] = True if local else ping_host(h["addr"])
    services = {}
    for svc, port in (h.get("ports") or {}).items():
        if local:
            services[svc] = {"port": port, "up": tcp_probe("127.0.0.1", port)}
        else:
            services[svc] = {"port": port, "up": tcp_probe(h["addr"], port) if st["ping"] else False}
    st["services"] = services
    st["hermes_dashboard"] = services.get("dashboard", {}).get("up", False)
    if local:
        st["ssh_up"] = True
        st["ssh_ok"] = True
        st["os_hint"] = "your-hub (hub — local probes)"
        st["state"] = "ONLINE"
        return st
    ssh_up = tcp_probe(h["addr"], 22) if st["ping"] else False
    st["ssh_up"] = ssh_up
    if ssh_up and not st["pending"]:
        # universal probe command: cd to a guaranteed dir + hostname —
        # valid in cmd.exe AND every POSIX shell, so no OS sniffing needed
        rc = subprocess.run(
            ["ssh", "-i", str(IDENTITY), "-o", "BatchMode=yes", "-o",
             "ConnectTimeout=6", f"{h['user']}@{h['addr']}", "cd . && hostname"],
            capture_output=True, text=True, timeout=20)
        st["ssh_ok"] = rc.returncode == 0
        hint = " ".join((rc.stdout or "").split())
        st["os_hint"] = hint[:60]
    else:
        st["ssh_ok"] = False
        st["os_hint"] = ""
    st["state"] = ("PENDING" if st["pending"]
                   else "ONLINE" if (st["ping"] and st["ssh_ok"])
                   else "REACHABLE" if st["ping"]
                   else "DOWN")
    return st

def probe_all():
    hosts = load_hosts()
    results = {}
    threads = []
    lock = threading.Lock()
    def _t(n, h):
        r = probe_host(n, h)
        with lock:
            results[n] = r
    for n, h in hosts.items():
        t = threading.Thread(target=_t, args=(n, h)); t.start(); threads.append(t)
    for t in threads: t.join(timeout=30)
    return results

# ---------------------------------------------------------------- actions

def wol(mac):
    """Wake-on-LAN: 6x FF + 16x MAC, broadcast on port 9."""
    if not mac:
        return False, "no MAC registered for this host"
    try:
        pkt = b"\xff" * 6 + bytes.fromhex(mac.replace(":", "").replace("-", "")) * 16
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(pkt, ("255.255.255.255", 9))
        # also directed broadcast to the /24 in case the router filters global bcast
        s.sendto(pkt, ("your-lan-broadcast", 9))
        s.close()
        return True, f"magic packet sent to {mac}"
    except Exception as e:
        return False, str(e)[:120]

def engine_cmd(op, host, timeout=240):
    """Run the real sync engine (push/pull). Returns (ok, output).
    The hub (method=local) is its own mirror — files are already at the hub path,
    so report the truth instead of sshing to ourselves."""
    h = load_hosts().get(host, {})
    if h.get("method") == "local":
        root = Path(h.get("remote_root", ""))
        n = sum(1 for _ in root.rglob("*") if _.is_file()) if root.is_dir() else 0
        return True, f"hub mirror: {host} IS the sync hub — {n} files live at {root}"
    r = subprocess.run(["python3", str(ENGINE), op, host],
                       capture_output=True, text=True, timeout=timeout)
    out = (r.stdout + r.stderr).strip()
    return r.returncode == 0, out[-1500:]

def host_task_status(host):
    """Query the heartbeat task on a Windows host (schtasks)."""
    h = load_hosts().get(host, {})
    if not h.get("addr"):
        return {"error": "unknown host"}
    r = subprocess.run(
        ["ssh", "-i", str(IDENTITY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
         f"{h['user']}@{h['addr']}", "cmd /c schtasks /Query /TN OperatorFleetPull /V /FO LIST"],
        capture_output=True, text=True, timeout=25)
    if r.returncode != 0:
        return {"error": (r.stderr or "query failed")[:120]}
    out = {}
    for line in r.stdout.splitlines():
        if "Last Result" in line:
            out["last_result"] = line.split(":", 1)[-1].strip()
        if "Last Run Time" in line:
            out["last_run"] = line.split(":", 1)[-1].strip()  # first-colon split: time values contain colons
        if "Status:" in line:
            out["status"] = line.split(":", 1)[-1].strip()
    out["raw"] = True
    return out

def host_task_fire(host):
    h = load_hosts().get(host, {})
    r = subprocess.run(
        ["ssh", "-i", str(IDENTITY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
         f"{h['user']}@{h['addr']}", "cmd /c schtasks /Run /TN OperatorFleetPull"],
        capture_output=True, text=True, timeout=25)
    ok = r.returncode == 0 and "SUCCESS" in (r.stdout + r.stderr).upper()
    return ok, (r.stdout + r.stderr).strip()[-120:]

# ---------------------------------------------------------------- in-flight op registry

OPS = {}  # id -> {"kind","host","state","started","result"}
OPS_LOCK = threading.Lock()

def launch_op(kind, host=None, target=None, params=None):
    oid = f"{int(time.time()*1000)}"
    op = {"id": oid, "kind": kind, "host": host or target or "fleet",
          "state": "running", "started": utcnow(), "result": ""}
    def _wrap(fn, *a):
        try:
            ok, msg = fn(*a)
            op["state"] = "done" if ok else "failed"
            op["result"] = msg
        except Exception as e:
            op["state"] = "failed"
            op["result"] = str(e)[:300]
        op["finished"] = utcnow()
    with OPS_LOCK:
        OPS[oid] = op
    if kind == "probe_all":
        threading.Thread(target=_wrap, args=(lambda: (_probe_store(probe_all()), "fleet probe complete"),), daemon=True).start()
    elif kind == "wake":
        if host == "your-hub":
            threading.Thread(target=_wrap, args=(lambda: (True, "your-hub is the hub — always awake"),), daemon=True).start()
        else:
            mac = load_hosts().get(host, {}).get("mac")
            threading.Thread(target=_wrap, args=(wol, mac), daemon=True).start()
    elif kind in ("push", "pull"):
        threading.Thread(target=_wrap, args=(engine_cmd, kind, host), daemon=True).start()
    elif kind == "fire_task":
        threading.Thread(target=_wrap, args=(host_task_fire, host), daemon=True).start()
    elif kind == "thunder":
        threading.Thread(target=_wrap, args=(thunder, host), daemon=True).start()
    elif kind == "spectrum":
        def _spec():
            ok, data = spectrum()
            LAST_SPECTRUM["at"] = utcnow()
            LAST_SPECTRUM["data"] = data
            return ok, f"telemetry harvested from {len(data)} hosts"
        threading.Thread(target=_wrap, args=(_spec,), daemon=True).start()
    elif kind == "forge_submit":
        acts = (params or {}).get("acts") or ["body_worship"]
        cycles = (params or {}).get("cycles", 1)
        threading.Thread(target=_wrap, args=(forge_submit, acts, cycles), daemon=True).start()
    elif kind == "forge_fetch":
        threading.Thread(target=_wrap, args=(forge_fetch,), daemon=True).start()
    return op

def _ts_less_than_60s(ts):
    try:
        from datetime import datetime as _dt
        t = _dt.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() < 60
    except Exception:
        return False

LAST_PROBE = {"at": None, "hosts": {}}
def _probe_store(results):
    LAST_PROBE["at"] = utcnow()
    LAST_PROBE["hosts"] = results
    return results

# ================================================================ TOOL 1: THUNDER (fleet broadcast)
# One command, every live host, per-OS mapped, aggregated. your-hub is the thundercloud.

PS_WRAP = 'powershell -NoProfile -Command "{}"'
OS_CMD = {
    "win_shutdown":  ("cmd /c shutdown /s /t 30 /c \"Fleet command: shutdown in 30s\"", True),
    "win_reboot":    ("cmd /c shutdown /r /t 30 /c \"Fleet command: reboot in 30s\"", True),
    "win_logoff":    ("cmd /c logoff", True),
    "win_updates":   (PS_WRAP.format("Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 3 HotFixID,InstalledOn | ConvertTo-Json -Compress"), True),
    "win_uptime":    (PS_WRAP.format("$o=Get-CimInstance Win32_OperatingSystem; Write-Output ('uptime_days=' + [math]::Round(((Get-Date)-$o.LastBootUpTime).TotalDays,1))"), True),
    "win_listening": (PS_WRAP.format("(Get-NetTCPConnection -State Listen).Count"), True),
    "lin_uptime":    ("uptime -p && df -h / | tail -1", False),
    "lin_updates":   ("apt list --upgradable 2>/dev/null | head -6", False),
    "lin_disk":      ("df -h / | tail -1", False),
    "lin_netstat":   ("ss -tln | tail -n +2 | wc -l", False),
}

def _host_is_win(h):
    return h.get("remote_root", "").startswith(("C:", "D:"))

def thunder(cmd_key):
    """Run a mapped command on every LIVE host in parallel; collect everything."""
    spec = OS_CMD.get(cmd_key)
    if not spec:
        return False, f"unknown broadcast '{cmd_key}'. known: {', '.join(OS_CMD)}"
    hosts = {n: h for n, h in load_hosts().items() if h.get("status", "LIVE").upper() != "PENDING"}
    cmd, wants_win = spec
    results = {}
    lock = threading.Lock()
    def _one(name, h):
        try:
            if h.get("method") == "local":
                if wants_win:
                    with lock:
                        results[name] = {"ok": False, "out": "windows-only command on linux hub", "rc": -1}
                else:
                    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=25)
                    with lock:
                        results[name] = {"ok": r.returncode == 0, "out": (r.stdout + r.stderr).strip()[:400] or "(no output)", "rc": r.returncode}
                return
            r = subprocess.run(
                ["ssh", "-i", str(IDENTITY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                 f"{h['user']}@{h['addr']}", cmd],
                capture_output=True, text=True, timeout=45)
            out = "\n".join(l for l in (r.stdout or "").splitlines()
                            if "post-quantum" not in l and "decrypt later" not in l and "openssh.com" not in l)
            with lock:
                results[name] = {"ok": r.returncode == 0, "out": out.strip()[:400] or "(no output)",
                                 "rc": r.returncode}
        except Exception as e:
            with lock:
                results[name] = {"ok": False, "out": str(e)[:200], "rc": -1}
    threads = []
    for n, h in hosts.items():
        if wants_win and not _host_is_win(h):
            with lock:
                results[n] = {"ok": True, "out": "(skipped: Linux host, Windows command)", "rc": "skip"}
            continue
        if not wants_win and _host_is_win(h):
            with lock:
                results[n] = {"ok": True, "out": "(skipped: Windows host, Linux command)", "rc": "skip"}
            continue
        t = threading.Thread(target=_one, args=(n, h)); t.start(); threads.append(t)
    for t in threads: t.join(timeout=50)
    return len(results) > 0, json.dumps(results, indent=1)[:1800]

# ================================================================ TOOL 2: SPECTRUM (combined telemetry)
# Parallel metric harvest from every member -> one fleet pulse. GPU from your-render-rig, CPU/RAM/disk from all.

def _ssh_out(h, cmd, timeout=25):
    r = subprocess.run(
        ["ssh", "-i", str(IDENTITY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
         f"{h['user']}@{h['addr']}", cmd],
        capture_output=True, text=True, timeout=timeout)
    return r.stdout or ""

def spectrum():
    hosts = load_hosts()
    out = {}
    lock = threading.Lock()
    def _one(name, h):
        m = {"addr": h["addr"], "label": h.get("label", name), "status": h.get("status", "LIVE")}
        try:
            if h.get("method") == "local":
                # hub: read /proc directly, no shell
                cpu = open("/proc/stat").readline()
                loads = [float(x) for x in cpu.split()[1:]]
                import time as _t; _t.sleep(0.15)
                cpu2 = open("/proc/stat").readline()
                l2 = [float(x) for x in cpu2.split()[1:]]
                deltas = [b - a for a, b in zip(loads, l2)]
                m["cpu"] = round(100 * (1 - deltas[3] / max(sum(deltas), 1)), 1)
                meminfo = {}
                for line in open("/proc/meminfo"):
                    k, v = line.split(":")
                    meminfo[k] = int(v.strip().split()[0])
                m["ram"] = round(100 * (1 - meminfo["MemAvailable"] / meminfo["MemTotal"]), 1)
                st_ = os.statvfs("/")
                m["disk"] = round(100 * (1 - st_.f_bavail / st_.f_blocks), 1)
                m["gpu"] = None
                out[name] = m
                return
            if _host_is_win(h):
                # -EncodedCommand: base64(UTF-16LE) script survives cmd.exe + ssh + every
                # shell layer unmangled (learned the hard way — single-quote wrapping
                # worked on your-render-rig but laptop-1's default shell ate the quotes).
                import base64 as _b64
                script = (
                    "$c=Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average; "
                    "$o=Get-CimInstance Win32_OperatingSystem; "
                    "$mem=[math]::Round(100-100*$o.FreePhysicalMemory/$o.TotalVisibleMemorySize,0); "
                    "$d=Get-CimInstance Win32_LogicalDisk | Where-Object DeviceID -eq \"C:\" | Select-Object -First 1; "
                    "$dd=[math]::Round(100-100*$d.FreeSpace/$d.Size,0); "
                    "Write-Output (\"cpu=\" + $c.Average + \" ram=\" + $mem + \" disk=\" + $dd)")
                enc = _b64.b64encode(script.encode("utf-16-le")).decode()
                raw = _ssh_out(h, f"powershell -NoProfile -EncodedCommand {enc}", timeout=35)
                kv = dict(p.split("=") for p in raw.split() if "=" in p)
                m.update(cpu=kv.get("cpu","?"), ram=kv.get("ram","?"), disk=kv.get("disk","?"), gpu=None)
            else:
                d = chr(36)
                lin = ("top -bn1 | grep 'Cpu(s)' | head -1 ; "
                       f"free -m | awk '/Mem:/{{print \"ram=\" int(100*{d}3/{d}2)}}'; "
                       "df -h / | awk 'NR==2{print \"disk=\" int($5)}'")
                raw = _ssh_out(h, lin)
                cpu = next((l.split(":")[1].split(",")[0].strip() for l in raw.splitlines() if "Cpu" in l), "?")
                kv = dict(p.split("=") for p in raw.replace(",", " ").split() if "=" in p)
                m.update(cpu=cpu, ram=kv.get("ram","?"), disk=kv.get("disk","?"), gpu=None)
        except Exception as e:
            m["error"] = str(e)[:120]
        out[name] = m
    threads = [threading.Thread(target=_one, args=(n, h)) for n, h in hosts.items()]
    for t in threads: t.start()
    for t in threads: t.join(timeout=40)

    # GPU layer: your-render-rig's 5090 (the fleet's heavy metal)
    z = load_hosts().get("render-rig", {})
    if z.get("addr"):
        try:
            g = _ssh_out(z, "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv,noheader", timeout=20)
            parts = [p.strip() for p in g.strip().split(",")]
            if len(parts) == 4:
                out["render-rig"]["gpu"] = {"util": parts[0], "vram_used": parts[1], "vram_total": parts[2], "temp": parts[3]}
        except Exception:
            pass
    # hub latency snapshot (your-hub's view of the fleet)
    for name, m in out.items():
        if name == "your-hub": continue
        r = subprocess.run(["ping", "-c", "1", "-W", "2", m["addr"]], capture_output=True, timeout=6)
        m["latency_ms"] = (r.returncode == 0)
        if r.returncode == 0:
            try:
                m["latency_ms"] = float(str(r.stdout).split("time=")[-1].split(" ms")[0])
            except Exception:
                pass
    return True, out

LAST_SPECTRUM = {"at": None, "data": {}}


# ================================================================ TOOL 4: ARMORY (hexstrike battle bridge + app launcher)
# Full red-team command of the your-hub box from any device: 90 hexstrike tools,
# raw command execution, process control, and every desktop app launchable.

HEXSTRIKE = "http://127.0.0.1:8888"
HEXSTRIKE_LAUNCHER = HOME / ".hermes/scripts/hexstrike_launch.sh"
ARMORY_CATALOG = {"at": None, "hexstrike_tools": [], "apps": []}

def _hexstrike_health():
    try:
        r = subprocess.run(["curl", "-s", "-m", "4", f"{HEXSTRIKE}/health"],
                           capture_output=True, text=True, timeout=8)
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        return None

def _hexstrike_proxy(route, body=None, method="POST", timeout=300):
    """Proxy a request to the hexstrike server. Long timeout: tools can run minutes."""
    cmd = ["curl", "-s", "-m", str(timeout), "-X", method, f"{HEXSTRIKE}{route}",
           "-H", "Content-Type: application/json"]
    if body is not None:
        cmd += ["-d", json.dumps(body)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
    try:
        return True, json.loads(r.stdout)
    except Exception:
        return False, {"error": (r.stdout or r.stderr or "empty response")[:300]}

def armory_catalog(force=False):
    if not force and ARMORY_CATALOG["at"] and ARMORY_CATALOG["hexstrike_tools"]:
        return ARMORY_CATALOG
    # hexstrike toolmap (built from server source scan)
    tm_path = HOME / "Empire/shared/hexstrike_toolmap.json"
    if tm_path.is_file():
        ARMORY_CATALOG["hexstrike_tools"] = json.loads(tm_path.read_text())
    health = _hexstrike_health()
    ARMORY_CATALOG["hexstrike_online"] = bool(health)
    ARMORY_CATALOG["health"] = health
    apps_path = HOME / "Empire/shared/app_catalog.json"
    if apps_path.is_file():
        ARMORY_CATALOG["apps"] = json.loads(apps_path.read_text())
    ARMORY_CATALOG["at"] = utcnow()
    return ARMORY_CATALOG

def armory_launch_app(app_file):
    """Launch a desktop app detached from the web-server process (so it survives)."""
    # validate against catalog to prevent arbitrary file execution
    apps = ARMORY_CATALOG.get("apps") or []
    known = any(a["file"] == app_file for a in apps)
    if not known:
        return False, f"app '{app_file}' not in catalog ({len(apps)} known)"
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    r = subprocess.Popen(
        ["bash", "-c", f"nohup gtk-launch {app_file} >/dev/null 2>&1 &"],
        env=env, start_new_session=True)
    time.sleep(1.5)
    return True, f"launched {app_file} (DISPLAY={env.get('DISPLAY')})"

def armory_hexstrike_start():
    if _hexstrike_health():
        return True, "hexstrike already online"
    subprocess.Popen(["bash", str(HEXSTRIKE_LAUNCHER)], start_new_session=True)
    # wait for health
    for _ in range(20):
        time.sleep(3)
        if _hexstrike_health():
            return True, "hexstrike server started and healthy"
    return False, "hexstrike failed to become healthy within 60s"

# ================================================================ TOOL 3: FORGE (GPU render bridge)
# Offload render jobs to your-render-rig's RTX 5090, watch the pipeline, pull artifacts
# back over the fleet fabric. your-hub orchestrates, your-render-rig renders, console shows progress.

FORGE_ROOT = "D:/Operator_Cum_Worship_Packs"
FORGE_JOBS = FORGE_ROOT + "/forge_jobs"
FORGE_SCRIPT = FORGE_ROOT + "/worship_motion_v2.py"   # proven render pipeline

def forge_submit(acts, cycles=1):
    """Queue a render: writes a job manifest on your-render-rig; its cron/render loop picks it up.
    Operator's pipeline is cron-driven, so we stage the request file the loop reads.
    Transfer = base64 -> [Convert]::FromBase64String: zero quoting hazards (JSON's
    double quotes get shredded by cmd.exe otherwise)."""
    manifest = {"requested_utc": utcnow(), "acts": acts, "cycles": cycles, "source": "forge-console"}
    b64 = base64.b64encode(json.dumps(manifest).encode()).decode()
    write_ps = ("powershell -NoProfile -Command \""
                f"New-Item -ItemType Directory -Force '{FORGE_JOBS}' | Out-Null; "
                f"[IO.File]::WriteAllText('{FORGE_JOBS}/job_request.json', "
                f"[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')))\"")
    r = subprocess.run(
        ["ssh", "-i", str(IDENTITY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
         "admin@your-render-box", write_ps], capture_output=True, text=True, timeout=30)
    # verify readback parses as the manifest we sent (write + verify, never assume)
    v = subprocess.run(
        ["ssh", "-i", str(IDENTITY), "-o", "BatchMode=yes", "admin@your-render-box",
         f"powershell -NoProfile -Command \"Get-Content '{FORGE_JOBS}/job_request.json' -Raw\""],
        capture_output=True, text=True, timeout=25)
    try:
        back = json.loads((v.stdout or "").strip())
        landed = back.get("source") == "forge-console" and back.get("acts") == acts
    except Exception:
        landed = False
    return (ok := r.returncode == 0) and landed, (
        f"job queued on RIG-01: acts={','.join(acts)} cycles={cycles}"
        if landed else f"queue failed: {(r.stderr or v.stdout or 'readback mismatch')[:150]}")

def forge_status():
    """Live pipeline view: last cycles from motion_v2 log + job queue state."""
    r = subprocess.run(
        ["ssh", "-i", str(IDENTITY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
         "admin@your-render-box",
         f"powershell -NoProfile -Command \"Get-Content {FORGE_ROOT}/motion_v2_log.jsonl -Tail 3\""],
        capture_output=True, text=True, timeout=25)
    log = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try: log.append(json.loads(line))
            except Exception: pass
    q = subprocess.run(
        ["ssh", "-i", str(IDENTITY), "-o", "BatchMode=yes", "admin@your-render-box",
         f"cmd /c if exist {FORGE_JOBS}\\job_request.json (cmd /c type {FORGE_JOBS}\\job_request.json) else (echo NO-JOB)"],
        capture_output=True, text=True, timeout=20)
    qtxt = (q.stdout or "").strip()
    return True, {"queue": "empty" if "NO-JOB" in qtxt else qtxt[:200],
                  "recent_cycles": log[-3:]}

def forge_fetch(name="latest"):
    """Pull newest artifact set back through the fabric into the hub."""
    # find newest cycle dir on your-render-rig
    r = subprocess.run(
        ["ssh", "-i", str(IDENTITY), "-o", "BatchMode=yes", "admin@your-render-box",
         f"powershell -NoProfile -Command \"(Get-ChildItem {FORGE_ROOT}/motion_v2 -Directory | Sort-Object Name -Descending | Select-Object -First 1).Name\""],
        capture_output=True, text=True, timeout=20)
    cyc = (r.stdout or "").strip().splitlines()[-1].strip() if (r.stdout or "").strip() else ""
    if not cyc:
        return False, "no cycle dirs found on your-render-rig"
    src = f"admin@your-render-box:{FORGE_ROOT}/motion_v2/{cyc}/*"
    dst = f"{HOME}/Empire/shared/forge_artifacts/{cyc}"
    os.makedirs(dst, exist_ok=True)
    p = subprocess.run(["scp", "-i", str(IDENTITY), "-o", "BatchMode=yes", src, dst + "/"],
                       capture_output=True, text=True, timeout=300)
    files = os.listdir(dst)
    if p.returncode == 0 and files:
        listing = ", ".join(f"{f} ({os.path.getsize(dst + '/' + f)//1024}KB)" for f in files[:6])
        return True, f"{cyc}: {len(files)} files -> hub Empire/shared/forge_artifacts/{cyc} [{listing}]"
    return False, (p.stderr or "scp failed")[:200]




# ================================================================ SECURITY LAYER (WebAuthn face-unlock + PIN recovery)
# Passkeys via WebAuthn: device secure-enclave signs challenges (face unlock on
# Operator's devices); server verifies ES256 signatures. Biometrics never leave devices.
# Origin pinned: https://fleet.local:9220 (mDNS + TLS). Recovery PIN as backup path.

import base64 as _b64
import hashlib as _hl
import hmac as _hmac
import secrets as _sec

AUTH_DB = HOME / ".hermes/fleet_auth.json"
SESSION_TTL = 60 * 60 * 8  # 8h
MAX_ATTEMPTS = 5

def _auth_db():
    if AUTH_DB.is_file():
        return json.loads(AUTH_DB.read_text())
    return {"credentials": [], "recovery_pin_hash": None, "failed": 0}

def _auth_save(db):
    AUTH_DB.write_text(json.dumps(db, indent=1))
    os.chmod(AUTH_DB, 0o600)

def _b64e(b): return _b64.urlsafe_b64encode(b).decode().rstrip("=")
def _b64d(s): return _b64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def _cbor_uint(major, val):
    if val < 24: return bytes([(major << 5) | val])
    if val < 256: return bytes([(major << 5) | 24, val])
    if val < 65536: return bytes([(major << 5) | 25, val >> 8, val & 0xFF])
    return bytes([(major << 5) | 26]) + val.to_bytes(4, "big")

def _cbor_bytes(b):
    h = len(b)
    if h < 24: return bytes([(0x40 | h)]) + b
    if h < 256: return bytes([0x58, h]) + b
    return bytes([0x58, 0xff]) + b  # not reached for our sizes

def _parse_attestation_object(fmt_bytes):
    """Minimal CBOR parse of authData from attestationObject.
    Layout: nonce(16) | flags(1) | signCount(4) | [AAGUID(16) | credIdLen(2) | credId | COSE pubkey]"""
    # flags at byte 32, sign count 33..36, then AT credential data if flag 0x40
    flags = fmt_bytes[32]
    sign_count = int.from_bytes(fmt_bytes[33:37], "big")
    out = {"flags": flags, "sign_count": sign_count, "cred_id": None, "pubkey_cose": None}
    if flags & 0x40:  # attested credential data present
        off = 37
        aaguid = fmt_bytes[off:off+16]; off += 16
        idlen = int.from_bytes(fmt_bytes[off:off+2], "big"); off += 2
        cred_id = fmt_bytes[off:off+idlen]; off += idlen
        out["cred_id"] = cred_id
        out["aaguid"] = aaguid.hex()
        # COSE key: CBOR map. Parse inline (minimal): expect 0xA3.. structure
        # algorithm -257 (ES256) => keys: 1(2=EC2), 3(-257), -1(1=EC2P256), -2(x32), -3(y32)
        cose = fmt_bytes[off:]
        out["pubkey_cose"] = _parse_cose_key(cose)
    return out

def _parse_cose_key(cose):
    """Extract raw EC x,y (32B each) + alg from a CBOR COSE_Key map. Minimal parser
    tuned to authenticator output: map header 0xA3/0xA5, int keys, byte-string vals."""
    def _read_int(b, i):
        v = b[i] & 0x1F; mi = b[i] >> 5
        if mi > 1: return None, i  # only uint (0) and negative (1) ints handled
        if v < 24: n = v; i += 1
        elif v == 24: n = b[i+1]; i += 2
        elif v == 25: n = int.from_bytes(b[i+1:i+3], "big"); i += 3
        elif v == 26: n = int.from_bytes(b[i+1:i+5], "big"); i += 5
        else: return None, i
        return (-1 - n) if mi == 1 else n, i
    def _read_bytes(b, i):
        v = b[i] & 0x1F; mi = b[i] >> 5
        if mi != 2: return None, i
        if v < 24: n = v; i += 1
        elif v == 24: n = b[i+1]; i += 2
        elif v == 25: n = int.from_bytes(b[i+1:i+3], "big"); i += 3
        elif v == 26: n = int.from_bytes(b[i+1:i+5], "big"); i += 5
        else: return None, i
        return b[i:i+n], i + n
    out = {}
    i = 0
    if cose[i] >> 5 != 5: return None  # not a map
    nkeys = cose[i] & 0x1F
    if cose[i] & 0x1F >= 24:
        nkeys = int.from_bytes(cose[i+1:i+1 + (2 if (cose[i]&0x1F)==25 else 4)], "big")
        i += 1 + (2 if (cose[i]&0x1F)==25 else 4)
    i += 1
    for _ in range(nkeys):
        k, i = _read_int(cose, i)
        if k in (1, 3, -1, -2, -3):
            if k in (-2, -3):
                val, i = _read_bytes(cose, i)
                out[k] = val
            else:
                v, i = _read_int(cose, i)
                out[k] = v
        else:
            # unknown: try int then bytes
            save = i
            v, ni = _read_int(cose, i)
            if v is not None: i = ni
            else:
                val, ni = _read_bytes(cose, save)
                if val is not None: i = ni
                else: break
    if out.get(1) == 2 and out.get(3) == -257 and out.get(-2) and out.get(-3):
        return {"kty": out[1], "alg": out[3], "x": out[-2], "y": out[-3]}
    return None

def _cose_from_xy(x, y):
    """Build CBOR COSE_Key for EC2/P-256/ES256 from raw coords.
    CBOR negative ints: major 1, encoded value n means -(n+1).
    Keys: 1:kty=2(EC2), 3:alg=-257(ES256 -> n=256), -1:crv=1(P-256 -> n=0), -2:x, -3:y"""
    out = bytes([0xA5])
    out += _cbor_uint(0, 1) + _cbor_uint(0, 2)   # 1: 2 (EC2)
    out += _cbor_uint(0, 3) + _cbor_uint(1, 256) # 3: -257 (ES256) — major1 n=256 -> -257
    out += _cbor_uint(1, 0) + _cbor_uint(0, 1)   # -1: 1 (P-256) — major1 n=0 -> -1
    out += _cbor_uint(1, 1) + _cbor_bytes(x)     # -2: x
    out += _cbor_uint(1, 2) + _cbor_bytes(y)     # -3: y
    return out

def _verify_es256(pub_xy, sig_rfc5480, data):
    """Verify ECDSA/P-256 signature (raw r||s or DER) over data using cryptography."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes
    from cryptography.exceptions import InvalidSignature
    try:
        pub = ec.EllipticCurvePublicNumbers(
            x=int.from_bytes(pub_xy[0], "big"),
            y=int.from_bytes(pub_xy[1], "big"),
            curve=ec.SECP256R1()).public_key()
        sig = sig_rfc5480
        if len(sig) == 64:  # raw r||s -> DER
            r, s = sig[:32], sig[32:]
            sig = _int_to_der(int.from_bytes(r, "big"), int.from_bytes(s, "big"))
        pub.verify(sig, data, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False

def _int_to_der(r, s):
    def _n(v):
        b = v.to_bytes((v.bit_length() + 7) // 8, "big")
        if b[0] & 0x80: b = b"\x00" + b
        return b
    r_b, s_b = _n(r), _n(s)
    body = b"\x02" + bytes([len(r_b)]) + r_b + b"\x02" + bytes([len(s_b)]) + s_b
    return b"0" + bytes([len(body)]) + body


def _cbor_find_key(cbor_bytes, wanted: str):
    """Crude scan: find UTF-8 string key in a CBOR map, return following byte-string value.
    Good enough for fixed WebAuthn attestation layout."""
    needle = wanted.encode()
    # text string major=3: header 0x60+len (or 0x78 len8). Search all occurrences.
    i = 0
    while i < len(cbor_bytes) - len(needle):
        if cbor_bytes[i] >> 5 == 3:
            v = cbor_bytes[i] & 0x1F
            n = v if v < 24 else (cbor_bytes[i+1] if v == 24 else 0)
            if cbor_bytes[i+ (2 if v==24 else 1): i + (2 if v==24 else 1) + n] == needle:
                j = i + (2 if v == 24 else 1) + n
                # value: byte string
                if cbor_bytes[j] >> 5 == 2:
                    v2 = cbor_bytes[j] & 0x1F
                    if v2 < 24: n2 = v2; j2 = j + 1
                    elif v2 == 24: n2 = cbor_bytes[j+1]; j2 = j + 2
                    elif v2 == 25: n2 = int.from_bytes(cbor_bytes[j+1:j+3], "big"); j2 = j + 3
                    else: return None
                    return cbor_bytes[j2:j2+n2]
        i += 1
    return None

def _challenge():
    ch = _sec.token_bytes(32)
    _PENDING_CHALLENGES[ch] = {"ts": time.time()}
    return ch

_PENDING_CHALLENGES = {}

def _challenge_pop(ch):
    rec = _PENDING_CHALLENGES.pop(ch, None)
    if not rec: return False
    return time.time() - rec["ts"] < 120  # 2 min validity

def _session_token():
    tok = _sec.token_urlsafe(32)
    _SESSIONS[tok] = time.time() + SESSION_TTL
    return tok

_SESSIONS = {}

def _session_valid(tok):
    exp = _SESSIONS.get(tok)
    if not exp: return False
    if time.time() > exp:
        _SESSIONS.pop(tok, None)
        return False
    return True


# ---------------------------------------------------------------- HTTP API

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence request spam
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode())))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body.encode())

    def _authed(self):
        tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
        return _session_valid(tok)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._html(PAGE)
        if path == "/api/state":
            hosts = load_hosts()
            return self._json({
                "version": VERSION, "now": utcnow(), "hub": "your-hub (this box)",
                "hosts": LAST_PROBE["hosts"] or {n: {"state": "UNKNOWN", "addr": h["addr"], "label": h.get("label", n)}
                                                 for n, h in hosts.items()},
                "probe_at": LAST_PROBE["at"],
                "audit": audit_summary(),
                "ops": list(OPS.values())[-12:][::-1],
                "spectrum": LAST_SPECTRUM,
            })
        if path == "/api/probe":
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            return self._json(probe_all())
        if path == "/api/spectrum":
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            # return cached telemetry; background-harvest if stale (>60s)
            fresh = LAST_SPECTRUM["at"] and _ts_less_than_60s(LAST_SPECTRUM["at"])
            if not fresh:
                launch_op("spectrum")
            return self._json({"at": LAST_SPECTRUM["at"], "data": LAST_SPECTRUM["data"],
                               "harvesting": not fresh})
        if path == "/api/audit":
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            return self._json({"rows": audit_rows(120)})
        if path == "/api/forge/status":
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            ok, data = forge_status()
            return self._json(data if ok else {"error": data})
        if path == "/api/armory/catalog":
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            return self._json(armory_catalog())
        if path == "/api/armory/processes":
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            ok, data = _hexstrike_proxy("/api/processes/dashboard", method="GET", timeout=20)
            return self._json(data if ok else {"error": data})
        if path.startswith("/api/armory/tool_status/"):
            tool = path.rsplit("/", 1)[-1]
            ok, data = _hexstrike_proxy(f"/api/tools/{tool}", body={}, timeout=30)
            return self._json(data)
        if path.startswith("/api/task_status/"):
            if not self._authed():
                return self._json({"error": "unauthorized"}, 401)
            return self._json(host_task_status(path.rsplit("/", 1)[-1]))
        if path == "/api/auth/state":
            db = _auth_db()
            tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
            return self._json({"enrolled": bool(db["credentials"]) or db.get("recovery_pin_hash"),
                               "session_valid": _session_valid(tok)})
        return self._json({"error": "unknown endpoint"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        host = path.rsplit("/", 1)[-1] if "/" in path else ""
        # security layer: all POSTs require session EXCEPT auth endpoints
        if not path.startswith("/api/auth/") and not self._authed():
            return self._json({"error": "unauthorized — authenticate first"}, 401)
        if path == "/api/probe_async":
            op = launch_op("probe_all")
        elif path.startswith("/api/thunder/"):
            op = launch_op("thunder", host)     # host slot carries the command key
        elif path.startswith("/api/wake/"):
            op = launch_op("wake", host)
        elif path.startswith("/api/push/"):
            op = launch_op("push", host)
        elif path.startswith("/api/pull/"):
            op = launch_op("pull", host)
        elif path.startswith("/api/fire_task/"):
            op = launch_op("fire_task", host)
        elif path == "/api/forge/submit":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 2)
            try:
                req = json.loads(body or b"{}")
            except Exception:
                req = {}
            acts = req.get("acts") or []
            cycles = int(req.get("cycles") or 1)
            op = launch_op("forge_submit", params={"acts": acts, "cycles": cycles})
        elif path == "/api/forge/fetch":
            op = launch_op("forge_fetch")
        elif path == "/api/armory/hexstrike/start":
            ok, msg = armory_hexstrike_start()
            return self._json({"ok": ok, "msg": msg})
        elif path.startswith("/api/armory/tool/"):
            tool = path.rsplit("/", 1)[-1]
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 2)
            try:
                req = json.loads(body or b"{}")
            except Exception:
                req = {}
            ok, data = _hexstrike_proxy(f"/api/tools/{tool}", body=req, timeout=600)
            return self._json(data, 200 if ok else 502)
        elif path == "/api/armory/command":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 2)
            try:
                req = json.loads(body or b"{}")
            except Exception:
                req = {}
            ok, data = _hexstrike_proxy("/api/command", body=req, timeout=600)
            return self._json(data, 200 if ok else 502)
        elif path.startswith("/api/armory/launch/"):
            app_file = path.rsplit("/", 1)[-1]
            ok, msg = armory_launch_app(app_file)
            return self._json({"ok": ok, "msg": msg})
        elif path.startswith("/api/armory/kill/"):
            pid = path.rsplit("/", 1)[-1]
            ok, data = _hexstrike_proxy(f"/api/processes/terminate/{pid}", body={}, timeout=20)
            return self._json(data)
        # ---- security layer (POST auth routes follow) ----
        elif path == "/api/auth/webauthn/register_begin":
            db = _auth_db()
            ch = _challenge()
            rp_id = "fleet.local"
            return self._json({
                "publicKey": {
                    "challenge": _b64e(ch),
                    "rp": {"id": rp_id, "name": "Fleet Command Center"},
                    "user": {"id": _b64e(b"operator-fleet-admin"), "name": "Operator", "displayName": "Operator"},
                    "pubKeyCredParams": [{"type": "public-key", "alg": -257}],
                    "authenticatorSelection": {"authenticatorAttachment": "platform",
                                               "residentKey": "preferred", "userVerification": "required"},
                    "timeout": 120000, "attestation": "none"
                }})
        elif path == "/api/auth/webauthn/register_finish":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 2)
            try:
                req = json.loads(body or b"{}")
            except Exception:
                return self._json({"error": "bad body"}, 400)
            ch = _b64d(req.get("challenge", ""))
            if not _challenge_pop(ch):
                return self._json({"error": "challenge invalid/expired"}, 400)
            client_json = json.loads(req["clientDataJSON"])
            if client_json.get("challenge") != _b64e(ch):
                return self._json({"error": "challenge mismatch"}, 400)
            if client_json.get("origin") != ORIGIN:
                return self._json({"error": f"origin mismatch: {client_json.get('origin')}"}, 400)
            att = _b64d(req["attestationObject"])
            # minimal CBOR map walk: find "attStmt"(3) & "authData"(2) string keys
            auth_data = _cbor_find_key(att, "authData")
            if not auth_data:
                return self._json({"error": "authData missing"}, 400)
            parsed = _parse_attestation_object(auth_data)
            if not parsed.get("cred_id") or not parsed.get("pubkey_cose"):
                return self._json({"error": "no attested credential (platform authenticator required)"}, 400)
            db = _auth_db()
            db["credentials"].append({
                "cred_id": _b64e(parsed["cred_id"]),
                "x": _b64e(parsed["pubkey_cose"]["x"]),
                "y": _b64e(parsed["pubkey_cose"]["y"]),
                "sign_count": parsed["sign_count"],
                "added": utcnow(), "label": req.get("label", "device"),
            })
            db["failed"] = 0
            _auth_save(db)
            tok = _session_token()
            return self._json({"ok": True, "token": tok, "session_hours": SESSION_TTL // 3600})
        elif path == "/api/auth/webauthn/assert_begin":
            db = _auth_db()
            if not db["credentials"]:
                return self._json({"error": "no credentials enrolled"}, 400)
            ch = _challenge()
            return self._json({"publicKey": {
                "challenge": _b64e(ch), "rpId": "fleet.local", "timeout": 120000,
                "userVerification": "required",
                "allowCredentials": [{"type": "public-key", "id": c["cred_id"]} for c in db["credentials"]]}})
        elif path == "/api/auth/webauthn/assert_finish":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 2)
            try:
                req = json.loads(body or b"{}")
            except Exception:
                return self._json({"error": "bad body"}, 400)
            db = _auth_db()
            if db.get("failed", 0) >= MAX_ATTEMPTS:
                return self._json({"error": "locked out — restart server to reset"}, 423)
            ch = _b64d(req.get("challenge", ""))
            if not _challenge_pop(ch):
                return self._json({"error": "challenge invalid/expired"}, 400)
            client_json = json.loads(req["clientDataJSON"])
            if client_json.get("challenge") != _b64e(ch) or client_json.get("origin") != ORIGIN:
                db["failed"] = db.get("failed", 0) + 1; _auth_save(db)
                return self._json({"error": "challenge/origin mismatch"}, 400)
            auth_data = _b64d(req["authenticatorData"])
            sig = _b64d(req["signature"])
            cred_id_b64 = req.get("credentialId", "")
            cred = next((c for c in db["credentials"] if c["cred_id"] == cred_id_b64), None)
            if not cred:
                db["failed"] = db.get("failed", 0) + 1; _auth_save(db)
                return self._json({"error": "unknown credential"}, 400)
            signed = auth_data + _hl.sha256(client_json.get("origin", "").encode()).digest()
            # signed payload = authData || SHA256(clientDataJSON raw as sent)
            raw_client = req.get("clientDataJSON", "").encode()
            signed = auth_data + _hl.sha256(raw_client).digest()
            xy = (_b64d(cred["x"]), _b64d(cred["y"]))
            if not _verify_es256(xy, sig, signed):
                db["failed"] = db.get("failed", 0) + 1; _auth_save(db)
                return self._json({"error": "signature verify FAILED"}, 401)
            # sign count monotonic check
            sc = int.from_bytes(auth_data[33:37], "big")
            if sc <= cred.get("sign_count", 0) and sc != 0:
                db["failed"] = db.get("failed", 0) + 1; _auth_save(db)
                return self._json({"error": "sign count regression — cloned authenticator?"}, 401)
            cred["sign_count"] = sc
            db["failed"] = 0
            _auth_save(db)
            tok = _session_token()
            return self._json({"ok": True, "token": tok, "session_hours": SESSION_TTL // 3600})
        elif path == "/api/auth/pin/setup":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 2)
            try:
                req = json.loads(body or b"{}")
            except Exception:
                req = {}
            pin = str(req.get("pin", ""))
            if len(pin) < 8:
                return self._json({"error": "PIN must be >= 8 characters"}, 400)
            db = _auth_db()
            db["recovery_pin_hash"] = _b64e(_hl.sha256(pin.encode()).digest())
            _auth_save(db)
            return self._json({"ok": True})
        elif path == "/api/auth/pin/login":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 2)
            try:
                req = json.loads(body or b"{}")
            except Exception:
                req = {}
            db = _auth_db()
            if db.get("failed", 0) >= MAX_ATTEMPTS:
                return self._json({"error": "locked out — restart server to reset"}, 423)
            pin_hash = _b64e(_hl.sha256(str(req.get("pin", "")).encode()).digest())
            expected = db.get("recovery_pin_hash")
            if expected and _hmac.compare_digest(pin_hash, expected):
                db["failed"] = 0; _auth_save(db)
                tok = _session_token()
                return self._json({"ok": True, "token": tok, "session_hours": SESSION_TTL // 3600})
            db["failed"] = db.get("failed", 0) + 1; _auth_save(db)
            return self._json({"error": "wrong PIN"}, 401)
        else:
            return self._json({"error": "unknown action"}, 404)
        return self._json({"op": op})

def main():
    import ssl
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9220
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    cert = HOME / ".hermes/certs/fleet_cert.pem"
    key = HOME / ".hermes/certs/fleet_key.pem"
    if cert.is_file() and key.is_file():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        print(f"Fleet Command Center v{VERSION} -> https://fleet.local:{port} (TLS on)", flush=True)
    else:
        print(f"Fleet Command Center v{VERSION} -> http://your-hub.local:{port} (NO TLS — passkeys disabled)", flush=True)
    srv.serve_forever()

if __name__ == "__main__":
    import sys
    main()
