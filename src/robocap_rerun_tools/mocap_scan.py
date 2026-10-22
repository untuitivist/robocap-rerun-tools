"""Bound filesystem reads in a disposable worker, including blocking Windows SMB calls."""

from __future__ import annotations

import json
import os
import queue
import stat
import subprocess
import sys
import threading
import time


def list_directory(path):
    """Only this worker touches the filesystem; DirEntry reuses Windows find metadata."""
    directories, files, errors = [], [], []
    attributes = os.lstat(path)
    if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, "st_file_attributes", 0) & 0x400:
        raise ValueError("链接目录未跟随")
    with os.scandir(path) as entries:
        for entry in entries:
            try:
                attributes = entry.stat(follow_symlinks=False)
                if (
                    stat.S_ISLNK(attributes.st_mode)
                    or getattr(attributes, "st_file_attributes", 0) & 0x400
                ):
                    errors.append({"path": entry.path, "error": "链接未跟随"})
                elif stat.S_ISDIR(attributes.st_mode):
                    directories.append(entry.name)
                else:
                    files.append(entry.name.casefold())
            except OSError as exc:
                errors.append({"path": entry.path, "error": str(exc)})
    return {"directories": directories, "files": files, "errors": errors}


def worker():
    for line in sys.stdin:
        try:
            path = json.loads(line)
            result = list_directory(path)
        except (OSError, ValueError) as exc:
            result = {"error": str(exc)}
        print(json.dumps(result, ensure_ascii=False), flush=True)


class DirectoryReader:
    """One persistent worker per root; a stuck directory is skipped, not the whole root."""

    def __init__(self, *, timeout=30, progress_seconds=5, command=None):
        if timeout <= 0 or progress_seconds <= 0:
            raise ValueError("扫描超时和进度间隔必须大于零")
        self.timeout = timeout
        self.progress_seconds = progress_seconds
        self.command = command or [sys.executable, "-u", "-m", __name__, "--worker"]
        self.process = None
        self.thread = None
        self.messages = None

    def start(self):
        self.messages = queue.Queue()
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        messages, stream = self.messages, self.process.stdout

        def receive():
            try:
                for line in stream:
                    messages.put(line)
            finally:
                messages.put(None)

        self.thread = threading.Thread(target=receive, daemon=True)
        self.thread.start()

    def close(self):
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        self.thread.join(timeout=1)
        process.stdin.close()
        if not self.thread.is_alive():
            process.stdout.close()
        self.process = None

    def read(self, path, waiting=lambda _path, _elapsed: None):
        if self.process is None:
            self.start()
        started = time.monotonic()
        try:
            self.process.stdin.write(json.dumps(str(path), ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            while True:
                remaining = self.timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError(f"目录读取超过 {self.timeout:g} 秒，已跳过；搜索不完整")
                try:
                    line = self.messages.get(timeout=min(self.progress_seconds, remaining))
                except queue.Empty:
                    waiting(path, time.monotonic() - started)
                    continue
                if line is None:
                    raise OSError("扫描子进程意外退出")
                return json.loads(line)
        except (OSError, ValueError, TimeoutError):
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("Internal directory worker")
    worker()
