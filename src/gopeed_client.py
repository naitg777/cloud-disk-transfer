#!/usr/bin/env python3
"""
Gopeed 下载器的 HTTP API 客户端

只依赖标准库。Gopeed 以 systemd 服务常驻，监听 127.0.0.1:9999。

注意：Gopeed 的 -w 白名单必须配置为**精确路径**（如 /data/gopeed/downloads）。
使用 glob（如 /data/gopeed/*）虽然能通过创建任务时的校验，但下载会静默失败。
"""

import json
import urllib.error
import urllib.request


class GopeedError(RuntimeError):
    """Gopeed API 返回的业务错误"""


class GopeedClient:
    def __init__(self, base: str = "http://127.0.0.1:9999"):
        self.base = base.rstrip("/")

    # ---------- 底层 ----------
    def _api(self, path: str, method: str = "GET", body=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data,
            headers={"Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise GopeedError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}") from exc
        except urllib.error.URLError as exc:
            raise GopeedError(f"无法连接 Gopeed：{exc.reason}") from exc

        if payload.get("code") != 0:
            raise GopeedError(payload.get("msg") or f"未知错误 code={payload.get('code')}")
        return payload.get("data")

    # ---------- 任务 ----------
    def add_task(self, url: str, name: str, path: str,
                 headers: dict[str, str] | None = None,
                 connections: int | None = None) -> str:
        """创建下载任务，返回 task id

        connections: 分片连接数。Gopeed 会从 1 条连接逐步翻倍爬坡，
        实测吞吐基本正比于最终连接数（16 条约 0.34 MB/s、32 条约 1.27 MB/s），
        所以大文件应当显式指定更高的值，而不是吃全局默认。
        """
        extra = {}
        if headers:
            extra["header"] = headers
        opts = {"name": name, "path": path}
        if connections:
            opts["extra"] = {"connections": int(connections)}
        payload = {
            "req": {"url": url, "extra": extra},
            "opts": opts,
        }
        tid = self._api("/api/v1/tasks", "POST", payload)
        if not tid:
            raise GopeedError("创建任务未返回 task id")
        return tid

    def get_task(self, tid: str) -> dict:
        return self._api(f"/api/v1/tasks/{tid}") or {}

    def list_tasks(self) -> list:
        data = self._api("/api/v1/tasks")
        if isinstance(data, dict):
            return data.get("tasks") or []
        return data or []

    def remove_task(self, tid: str, force: bool = True) -> None:
        suffix = "?force=true" if force else ""
        self._api(f"/api/v1/tasks/{tid}{suffix}", "DELETE")

    def is_done(self, task: dict) -> bool:
        return task.get("status") == "done"

    def is_failed(self, task: dict) -> bool:
        return task.get("status") in ("error", "removed")

    def progress_of(self, task: dict) -> tuple[int, int, int]:
        """返回 (已下载字节, 总字节, 速度 B/s)"""
        p = task.get("progress") or {}
        meta_res = (task.get("meta") or {}).get("res") or {}
        total = meta_res.get("size") or task.get("size") or 0
        return (p.get("downloaded") or 0, total, p.get("speed") or 0)

    # ---------- 配置 ----------
    def get_config(self) -> dict:
        return self._api("/api/v1/config") or {}

    def set_connections(self, n: int) -> None:
        cfg = self.get_config()
        cfg.setdefault("protocolConfig", {}).setdefault("http", {})["connections"] = n
        self._api("/api/v1/config", "PUT", cfg)

    def info(self) -> dict:
        return self._api("/api/v1/info") or {}
