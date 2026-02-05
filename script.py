import os
import re
import time
import argparse
from typing import Any, Dict, List, Optional, Tuple

import psutil

def get_steam_path_windows() -> Optional[str]:
    try:
        import winreg
    except Exception:
        return None

    candidates = [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ]
    for root, key, value in candidates:
        try:
            with winreg.OpenKey(root, key) as k:
                p, _ = winreg.QueryValueEx(k, value)
                if isinstance(p, str) and os.path.isdir(p):
                    return os.path.normpath(p)
        except OSError:
            pass
    return None

_token_re = re.compile(r'"([^"]*)"|(\{)|(\})', re.MULTILINE)

def parse_keyvalues(text: str) -> Dict[str, Any]:
    tokens: List[str] = []
    for m in _token_re.finditer(text):
        if m.group(1) is not None:
            tokens.append(m.group(1))
        elif m.group(2) is not None:
            tokens.append("{")
        elif m.group(3) is not None:
            tokens.append("}")

    i = 0

    def parse_object() -> Dict[str, Any]:
        nonlocal i
        obj: Dict[str, Any] = {}
        while i < len(tokens):
            tok = tokens[i]
            if tok == "}":
                i += 1
                return obj
            key = tok
            i += 1
            if i >= len(tokens):
                break
            nxt = tokens[i]
            if nxt == "{":
                i += 1
                obj[key] = parse_object()
            else:
                obj[key] = nxt
                i += 1
        return obj

    return parse_object()

def load_kv_file(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return parse_keyvalues(f.read())
    except Exception:
        return {}

def get_library_paths(steam_path: str) -> List[str]:
    libs = {os.path.normpath(steam_path)}
    vdf_path = os.path.join(steam_path, "steamapps", "libraryfolders.vdf")
    kv = load_kv_file(vdf_path)

    lf = kv.get("libraryfolders")
    if isinstance(lf, dict):
        for _, v in lf.items():
            if isinstance(v, dict):
                p = v.get("path")
                if isinstance(p, str) and os.path.isdir(p):
                    libs.add(os.path.normpath(p))
    return sorted(libs)

def find_active_downloads(libs: List[str]) -> List[Tuple[str, str]]:
    active: List[Tuple[str, str]] = []
    for lib in libs:
        downloading = os.path.join(lib, "steamapps", "downloading")
        if not os.path.isdir(downloading):
            continue
        try:
            for name in os.listdir(downloading):
                p = os.path.join(downloading, name)
                if name.isdigit() and os.path.isdir(p):
                    active.append((name, lib))
        except OSError:
            pass
    return active

def get_app_manifest_path(appid: str, lib: str) -> str:
    return os.path.join(lib, "steamapps", f"appmanifest_{appid}.acf")

def get_app_info(appid: str, lib: str) -> Tuple[str, int, int, str, int, int, int, int]:
    acf = get_app_manifest_path(appid, lib)
    kv = load_kv_file(acf)
    app = kv.get("AppState")
    if not isinstance(app, dict):
        return (f"AppID {appid}", 0, 0, "", 0, 0, 0, 0)

    def to_int(x: Any) -> int:
        try:
            return int(str(x))
        except Exception:
            return 0

    name = app.get("name") if isinstance(app.get("name"), str) else f"AppID {appid}"
    bd = to_int(app.get("BytesDownloaded"))
    btd = to_int(app.get("BytesToDownload"))
    sf = str(app.get("StateFlags", ""))

    bs = to_int(app.get("BytesStaged"))
    bts = to_int(app.get("BytesToStage"))
    bc = to_int(app.get("BytesCommitted"))
    sod = to_int(app.get("SizeOnDisk"))

    return (name, bd, btd, sf, bs, bts, bc, sod)

APPID_RE = re.compile(r"\b(?:AppID|appid)\s*[:=]?\s*(\d{3,10})\b", re.IGNORECASE)

PAUSE_PATTERNS = [
    re.compile(r"\b(paused|pause|pausing|suspend|suspended)\b", re.IGNORECASE),
    re.compile(r"\bstop(ped|ping)?\b.*\bdownload\b", re.IGNORECASE),
]
RESUME_PATTERNS = [
    re.compile(r"\b(resume|resuming|unpause|continuing)\b", re.IGNORECASE),
    re.compile(r"\bstart(ed|ing)?\b.*\bdownload\b", re.IGNORECASE),
    re.compile(r"\bdownloading\b", re.IGNORECASE),
]

SPEED_RE = re.compile(
    r"(?P<val>\d+(?:\.\d+)?)\s*(?P<unit>KB/s|MB/s|GB/s|Kbit/s|Mbit/s|Gbit/s)",
    re.IGNORECASE
)

def tail_lines(path: str, max_lines: int = 8000) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return lines[-max_lines:]
    except Exception:
        return []

def infer_status_from_logs(content_log_path: str, appid: str) -> Optional[str]:
    lines = tail_lines(content_log_path, max_lines=8000)
    for line in reversed(lines):
        m = APPID_RE.search(line)
        if not m or m.group(1) != appid:
            continue
        if any(p.search(line) for p in PAUSE_PATTERNS):
            return "PAUSED"
        if any(r.search(line) for r in RESUME_PATTERNS):
            return "DOWNLOADING"
    return None

def speed_to_bps(val: float, unit: str) -> float:
    u = unit.lower()
    if u.endswith("bit/s"):
        mult = {"kbit/s": 1024, "mbit/s": 1024**2, "gbit/s": 1024**3}[u]
        return (val * mult) / 8.0
    mult = {"kb/s": 1024, "mb/s": 1024**2, "gb/s": 1024**3}[u]
    return val * mult

def infer_speed_from_logs(content_log_path: str, appid: str) -> Optional[float]:
    lines = tail_lines(content_log_path, max_lines=8000)
    for line in reversed(lines):
        m_app = APPID_RE.search(line)
        if not m_app or m_app.group(1) != appid:
            continue
        m = SPEED_RE.search(line)
        if not m:
            continue
        try:
            val = float(m.group("val"))
            unit = m.group("unit")
            return speed_to_bps(val, unit)
        except Exception:
            return None
    return None

def format_speed(bps: float) -> str:
    if bps < 1024:
        return f"{bps:.0f} B/s"
    kb = bps / 1024
    if kb < 1024:
        return f"{kb:.1f} KB/s"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.2f} MB/s"
    gb = mb / 1024
    return f"{gb:.2f} GB/s"

def format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.2f} MB"
    gb = mb / 1024
    if gb < 1024:
        return f"{gb:.2f} GB"
    tb = gb / 1024
    return f"{tb:.2f} TB"

def format_progress(done: int, total: int) -> str:
    if total <= 0:
        return f"{format_bytes(done)}"
    pct = (done / total) * 100.0
    return f"{pct:.1f}% ({format_bytes(done)} / {format_bytes(total)})"

def safe_mtime(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except Exception:
        return None

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--minutes", type=int, default=5)
    args = ap.parse_args()

    steam_path = get_steam_path_windows()
    if not steam_path:
        print("Steam не найден в реестре.")
        return

    libs = get_library_paths(steam_path)
    content_log = os.path.join(steam_path, "logs", "content_log.txt")

    active = find_active_downloads(libs)
    if not active:
        print("Активных загрузок не найдено.")
        return

    appid, lib = active[0]
    name, bd, btd, sf, bs, bts, bc, sod = get_app_info(appid, lib)
    manifest_path = get_app_manifest_path(appid, lib)

    net0 = psutil.net_io_counters()
    t0 = time.time()

    last_bd = bd
    last_bs = bs

    no_download_progress_streak = 0
    no_net_streak = 0

    for minute_idx in range(1, args.minutes + 1):
        time.sleep(args.interval)

        active_now = find_active_downloads(libs)
        if not active_now:
            break

        appid_now, lib_now = active_now[0]
        if appid_now != appid or lib_now != lib:
            appid, lib = appid_now, lib_now
            name, bd, btd, sf, bs, bts, bc, sod = get_app_info(appid, lib)
            manifest_path = get_app_manifest_path(appid, lib)
            last_bd = bd
            last_bs = bs
            net0 = psutil.net_io_counters()
            t0 = time.time()
            no_download_progress_streak = 0
            no_net_streak = 0
            continue

        name, bd, btd, sf, bs, bts, bc, sod = get_app_info(appid, lib)

        bps_log = infer_speed_from_logs(content_log, appid) if os.path.isfile(content_log) else None

        net1 = psutil.net_io_counters()
        t1 = time.time()
        dt_net = max(1e-6, t1 - t0)
        recv_delta = max(0, net1.bytes_recv - net0.bytes_recv)
        bps_fallback = recv_delta / dt_net

        if recv_delta == 0:
            no_net_streak += 1
        else:
            no_net_streak = 0

        if bps_log is not None:
            bps = bps_log
        else:
            bps = bps_fallback

        log_status = infer_status_from_logs(content_log, appid) if os.path.isfile(content_log) else None

        if bd == last_bd:
            no_download_progress_streak += 1
        else:
            no_download_progress_streak = 0

        if log_status == "PAUSED":
            status = "ПАУЗА"
        elif log_status == "DOWNLOADING":
            status = "ЗАГРУЗКА"
        else:
            if no_net_streak >= 2 and no_download_progress_streak >= 2:
                status = "ОЖИДАНИЕ/СТОП"
            elif no_net_streak >= 2:
                status = "ОЖИДАНИЕ/СТОП"
            elif no_download_progress_streak >= 2:
                status = "ЗАГРУЗКА"
            else:
                status = "НЕИЗВЕСТНО"

        print(f"[{minute_idx}/{args.minutes}] {name} | {format_speed(bps)} | {format_progress(bd, btd)} | {status}")

        last_bd = bd
        last_bs = bs
        net0, t0 = net1, t1

if __name__ == "__main__":
    main()
