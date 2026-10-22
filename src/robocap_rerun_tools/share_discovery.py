"""Discover existing Windows SMB shares using only the current account's access."""

from __future__ import annotations

import ctypes
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def hosts_from_output(text):
    hosts = set(re.findall(r"\\\\([^\\\s]+)", text))
    for line in text.splitlines():
        match = re.match(r"\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f-]{17})\s+", line, re.IGNORECASE)
        if not match or match[2].lower() == "ff-ff-ff-ff-ff-ff":
            continue
        try:
            address = ipaddress.ip_address(match[1])
        except ValueError:
            continue
        if address.is_private and not address.is_loopback and not address.is_multicast:
            hosts.add(str(address))
    return hosts


def probe_host(host):
    """Runs in a disposable subprocess so stalled Windows SMB calls have a deadline."""
    from ctypes import wintypes

    class ShareInfo(ctypes.Structure):
        _fields_ = [
            ("name", wintypes.LPWSTR),
            ("type", wintypes.DWORD),
            ("remark", wintypes.LPWSTR),
        ]

    api = ctypes.WinDLL("Netapi32.dll")
    api.NetShareEnum.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    api.NetShareEnum.restype = wintypes.DWORD
    api.NetApiBufferFree.argtypes = [ctypes.c_void_p]
    api.NetApiBufferFree.restype = wintypes.DWORD

    def emit(path, error=""):
        print(json.dumps({"path": path, "error": error}, ensure_ascii=False), flush=True)

    names = {"F$"}
    resume = wintypes.DWORD()
    while True:
        buffer = ctypes.c_void_p()
        count, total = wintypes.DWORD(), wintypes.DWORD()
        status = api.NetShareEnum(
            "\\\\" + host,
            1,
            ctypes.byref(buffer),
            0xFFFFFFFF,
            ctypes.byref(count),
            ctypes.byref(total),
            ctypes.byref(resume),
        )
        try:
            if status not in {0, 234}:
                emit("\\\\" + host, f"无法枚举共享，Windows 错误 {status}；未更改权限")
                break
            for item in ctypes.cast(buffer, ctypes.POINTER(ShareInfo))[: count.value]:
                if item.type & 0xFFFF == 0 and not item.name.endswith("$"):
                    names.add(item.name)
        finally:
            if buffer:
                api.NetApiBufferFree(buffer)
        if status != 234:
            break
    for name in sorted(names):
        root = f"\\\\{host}\\{name}"
        try:
            with os.scandir(root):
                pass
            emit(root)
        except OSError as exc:
            emit(root, str(exc))


def discover_shares(log=print):
    """Query OS-known hosts, then visible disk shares and F$; never scan an IP subnet."""
    if os.name != "nt":
        raise ValueError("自动共享发现只支持 Windows")
    records, roots = [], []
    flags = subprocess.CREATE_NO_WINDOW

    def command(args):
        try:
            result = subprocess.run(
                args, capture_output=True, timeout=10, creationflags=flags, check=False
            )
            text = (result.stdout + result.stderr).decode("utf-8", errors="replace")
            return text, "" if result.returncode == 0 else text.strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return "", str(exc)

    commands = [["net.exe", "use"], ["net.exe", "view"], ["arp.exe", "-a"]]
    hosts = set()
    with ThreadPoolExecutor(max_workers=3) as pool:
        for args, (output, error) in zip(commands, pool.map(command, commands)):
            hosts.update(hosts_from_output(output))
            if error:
                records.append({"path": " ".join(args), "error": error})
    hosts.discard(socket.gethostname())
    if len(hosts) > 128:
        records.append(
            {"path": "网络发现", "error": "可见节点超过 128，仅检查前 128 个；可用 --root 补充"}
        )
    log(f"系统网络记录中找到 {len(hosts)} 个候选节点；仅检查现有共享和当前账号权限")

    def check(host):
        try:
            with socket.create_connection((host, 445), timeout=1):
                pass
        except OSError:
            return [{"path": "\\\\" + host, "error": "SMB 445 不可达，未尝试连接共享"}]
        args = [sys.executable, "-m", __name__, "--probe", host]
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                timeout=15,
                creationflags=flags,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                check=False,
            )
            output = result.stdout
            extra = (
                [] if result.returncode == 0 else [{"path": host, "error": "共享检查子进程未完成"}]
            )
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or b""
            extra = [
                {"path": host, "error": "共享枚举/访问超过 15 秒，已停止；请检查网络和共享权限"}
            ]
        except OSError as exc:
            return [{"path": host, "error": str(exc)}]
        rows = []
        for line in output.decode("utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                extra.append({"path": host, "error": "共享检查返回格式无效"})
        return rows + extra

    with ThreadPoolExecutor(max_workers=8) as pool:
        for host, rows in zip(sorted(hosts)[:128], pool.map(check, sorted(hosts)[:128])):
            for row in rows:
                records.append(row)
                if not row["error"]:
                    roots.append(Path(row["path"]))
                    log("发现可访问共享：" + row["path"])
                else:
                    log(f"网络发现 {host}：{row['path']}：{row['error']}")
    return list(dict.fromkeys(roots)), records


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--probe" or not re.fullmatch(r"[\w.-]+", sys.argv[2]):
        raise SystemExit("Expected --probe HOST")
    probe_host(sys.argv[2])
