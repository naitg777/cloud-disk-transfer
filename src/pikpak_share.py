#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PikPak 分享链接解析（不依赖账号即可下视频）。

接口与签名算法参考 alist/OpenList 的 pikpak_share 驱动：
  1) captcha/init  → captcha_token
  2) share/detail  → 递归列出文件（翻页）
  3) share/file_info → media 链接（未登录时只返回视频地址）
  4) 登录后 share/restore → 转存到自己网盘 → files/{id}?usage=FETCH → web_content_link

注意：
  * PikPak 对机房 IP 有屏蔽，返回 share_status=PROHIBITED 时无解（需住宅代理）；
  * 非视频文件必须有账号（refresh_token）才能拿到下载地址。
"""

import hashlib
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

CLIENT_ID = "YUMx5nI8ZU8Ap8pm"
CLIENT_VERSION = "2.0.0"
PACKAGE_NAME = "mypikpak.com"
SALTS = [
    "C9qPpZLN8ucRTaTiUMWYS9cQvWOE", "+r6CQVxjzJV6LCV", "F", "pFJRC",
    "9WXYIDGrwTCz2OiVlgZa90qpECPD6olt", "/750aCr4lm/Sly/c", "RB+DT/gZCrbV",
    "", "CyLsf7hdkIRxRm215hl", "7xHvLi2tOYP0Y92b", "ZGTXXxu8E/MIWaEDB+Sm/",
    "1UI3", "E7fP5Pfijd+7K+t6Tg/NhuLq0eEUVChpJSkrKxpO",
    "ihtqpG6FMt65+Xk+tWUH2", "NhXXU9rg4XXdzo7u5o",
]
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36")
API_HOST = "api-drive.mypikpak.com"
USER_HOST = "user.mypikpak.com"


class PikPakError(Exception):
    """PikPak 返回的业务错误"""


def captcha_sign(device_id: str, ts: str) -> str:
    """Web 端签名：对 15 个盐逐个 md5。"""
    s = CLIENT_ID + CLIENT_VERSION + PACKAGE_NAME + device_id + ts
    for salt in SALTS:
        s = hashlib.md5((s + salt).encode("utf-8")).hexdigest()
    return "1." + s


def parse_share(text: str) -> tuple:
    """从文本里提取 (share_id, 密码)。支持 mypikpak.com / .net 的 /s/ 链接。"""
    import re
    m = re.search(r"https?://[^\s]*mypikpak\.(?:com|net)/s/([A-Za-z0-9_-]+)", text or "")
    if not m:
        raise PikPakError("没找到 PikPak 分享链接（应形如 https://mypikpak.com/s/xxxx）")
    share_id = m.group(1)
    pwd = ""
    pm = re.search(r"(?:密码|提取码|pass_?code|pwd)\s*[:：=]?\s*([A-Za-z0-9]{4})", text or "",
                   re.I)
    if pm:
        pwd = pm.group(1)
    return share_id, pwd


class PikPakShare:
    def __init__(self, refresh_token: str = "", share_pwd: str = "",
                 device_id: str = ""):
        self.device_id = device_id or ("%032x" % random.getrandbits(128))
        self.captcha_token = ""
        self.pass_code_token = ""
        self.access_token = ""
        self.refresh_token = (refresh_token or "").strip().strip('"').strip("'")
        self.user_id = ""
        self.share_pwd = share_pwd

    # ---------- 底层 ----------
    def _headers(self) -> dict:
        h = {
            "User-Agent": DEFAULT_UA,
            "Content-Type": "application/json",
            "X-Client-ID": CLIENT_ID,
            "X-Device-ID": self.device_id,
            "X-Captcha-Token": self.captcha_token or "",
        }
        if self.access_token:
            h["Authorization"] = "Bearer " + self.access_token
        return h

    def _http(self, url: str, method: str = "GET", body=None,
              headers=None, timeout: int = 25) -> tuple:
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers or {},
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except Exception:
                return exc.code, {}
        except Exception as exc:
            return 0, {"err": str(exc)[:150]}

    def _captcha(self, action: str, meta: dict | None = None) -> str:
        ts = str(int(time.time() * 1000))
        m = {
            "captcha_sign": captcha_sign(self.device_id, ts),
            "client_version": CLIENT_VERSION,
            "package_name": PACKAGE_NAME,
            "timestamp": ts,
            "user_id": self.user_id or "",
        }
        if meta:
            m.update(meta)
        body = {
            "action": action,
            "captcha_token": self.captcha_token or "",
            "client_id": CLIENT_ID,
            "device_id": self.device_id,
            "meta": m,
            "redirect_uri": "xlaccsdk01://xbase.cloud/callback?state=harbor",
        }
        code, data = self._http(
            f"https://{USER_HOST}/v1/shield/captcha/init?client_id={CLIENT_ID}",
            "POST", body, self._headers())
        token = (data or {}).get("captcha_token")
        if not token:
            raise PikPakError(
                f"获取 captcha 失败：{code} {str(data)[:120]}")
        self.captcha_token = token
        return token

    def _request(self, path: str, method: str = "GET", query: dict | None = None,
                 body=None) -> dict:
        url = f"https://{API_HOST}{path}"
        if query:
            qs = urllib.parse.urlencode(
                {k: v for k, v in query.items() if v not in (None, "")})
            if qs:
                url += "?" + qs
        action = f"{method.upper()}:{path}"
        last = {}
        for attempt in range(3):
            if not self.captcha_token:
                self._captcha(action)
            code, data = self._http(url, method, body, self._headers())
            last = data or {}
            err = last.get("error_code")
            if err == 9:                     # 验证码失效
                self.captcha_token = ""
                continue
            if err and err in (16, 4121, 4122) and self.refresh_token:
                self._refresh_token()        # access_token 过期
                continue
            if code == 0:
                raise PikPakError(f"网络错误：{last.get('err')}")
            if err:
                raise PikPakError(
                    f"{last.get('error_description') or last.get('error') or err}")
            return last
        raise PikPakError(f"请求失败：{str(last)[:150]}")

    # ---------- 登录（可选，仅非视频文件需要） ----------
    def _refresh_token(self) -> None:
        code, data = self._http(
            f"https://{USER_HOST}/v1/auth/token?client_id={CLIENT_ID}", "POST",
            {"client_id": CLIENT_ID, "grant_type": "refresh_token",
             "refresh_token": self.refresh_token},
            {"User-Agent": DEFAULT_UA, "Content-Type": "application/json",
             "X-Client-ID": CLIENT_ID, "X-Device-ID": self.device_id})
        if not (data or {}).get("access_token"):
            raise PikPakError(
                f"refresh_token 失效：{(data or {}).get('error_description', str(data)[:100])}")
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token") or self.refresh_token
        self.user_id = data.get("sub") or ""
        self.captcha_token = ""

    def ensure_login(self) -> bool:
        if not self.refresh_token:
            return False
        self._refresh_token()
        return True

    # ---------- 业务 ----------
    def share_detail(self, share_id: str, parent_id: str = "",
                     page_token: str = "") -> dict:
        return self._request("/drive/v1/share/detail", "GET", {
            "share_id": share_id,
            "parent_id": parent_id or "",
            "pass_code_token": self.pass_code_token or "",
            "thumbnail_size": "SIZE_LARGE",
            "with_audit": "true",
            "limit": "100",
            "page_token": page_token or "",
            "filters": '{"phase":{"eq":"PHASE_TYPE_COMPLETE"},"trashed":{"eq":false}}',
        })

    def unlock(self, share_id: str) -> str:
        """有密码时换 pass_code_token。"""
        if not self.share_pwd:
            return ""
        d = self._request("/drive/v1/share", "GET", {
            "share_id": share_id, "pass_code": self.share_pwd,
            "thumbnail_size": "SIZE_LARGE", "limit": "100"})
        self.pass_code_token = d.get("pass_code_token") or ""
        return self.pass_code_token

    def file_info(self, share_id: str, file_id: str) -> dict:
        d = self._request("/drive/v1/share/file_info", "GET", {
            "share_id": share_id, "file_id": file_id,
            "pass_code_token": self.pass_code_token or ""})
        return d.get("file_info") or d

    def walk_share(self, share_id: str, limit: int = 5000) -> list:
        """递归列出分享内所有文件，返回 [{path,name,size,id,ancestors}]。"""
        out = []

        def walk(parent_id: str, prefix: str, ancestors: list, depth: int = 0):
            if depth > 12 or len(out) >= limit:
                return
            page_token = ""
            folders = []          # 跨页累积：翻页时不能把上一页的目录丢掉
            for _ in range(200):
                d = self.share_detail(share_id, parent_id, page_token)
                status = d.get("share_status") or ""
                if status in ("PASS_CODE_EMPTY", "PASS_CODE_ERROR"):
                    raise PikPakError("这个分享需要密码，请把密码一起发过来")
                if status and status != "OK":
                    if status == "PROHIBITED":
                        raise PikPakError(
                            "PikPak 拒绝了这台服务器的 IP（PROHIBITED），"
                            "需要住宅/代理出口才能解析")
                    raise PikPakError(
                        f"分享状态异常：{status} {d.get('share_status_text') or ''}")
                for f in d.get("files") or []:
                    if f.get("kind") == "drive#folder":
                        folders.append(f)
                        continue
                    path = f"{prefix}/{f.get('name', '')}" if prefix else f.get("name", "")
                    out.append({
                        "id": f.get("id"), "name": f.get("name", ""),
                        "path": path, "size": int(f.get("size") or 0),
                        "ancestors": list(ancestors),
                    })
                    if len(out) >= limit:
                        return
                page_token = d.get("next_page_token") or ""
                if not page_token:
                    break
            for folder in folders:          # 串行，降低请求频率
                sub = f"{prefix}/{folder.get('name', '')}" if prefix else folder.get("name", "")
                walk(folder.get("id"), sub, ancestors + [folder.get("id")], depth + 1)

        walk("", "", [])
        return out

    @staticmethod
    def pick_url(info: dict, prefer_transcode: bool = False) -> str:
        """从 file_info 里挑下载地址：优先 web_content_link，其次 medias。"""
        info = info or {}
        medias = [m for m in (info.get("medias") or []) if m.get("link", {}).get("url")]
        if prefer_transcode and len(medias) > 1:
            return medias[1]["link"]["url"]
        direct = info.get("web_content_link") or ""
        if direct:
            return direct
        if medias:
            return medias[0]["link"]["url"]
        return ""

    def download_headers(self) -> dict:
        return {"User-Agent": DEFAULT_UA, "Referer": "https://mypikpak.com/"}

    # ---------- 非视频：转存到自己网盘再取直链 ----------
    def restore(self, share_id: str, file_ids: list, ancestors: list) -> dict:
        if not self.access_token:
            raise PikPakError("非视频文件需要先登录 PikPak")
        body = {
            "share_id": share_id,
            "pass_code_token": self.pass_code_token or "",
            "file_ids": file_ids,
            "params": {"trace_file_ids": ",".join(file_ids)},
        }
        if ancestors:
            body["ancestor_ids"] = ancestors
        d = self._request("/drive/v1/share/restore", "POST", None, body)
        trace = (d.get("params") or {}).get("trace_file_ids")
        if d.get("restore_status") == "RESTORE_COMPLETE" and trace:
            return self._parse_trace(trace)
        task_id = d.get("restore_task_id")
        if not task_id:
            raise PikPakError(f"转存失败：{str(d)[:150]}")
        for _ in range(60):
            time.sleep(1)
            t = self._request(f"/drive/v1/tasks/{task_id}", "GET")
            phase = t.get("phase")
            if phase == "PHASE_TYPE_ERROR":
                raise PikPakError(
                    f"转存失败：{(t.get('params') or {}).get('error_detail') or t.get('message')}")
            if phase == "PHASE_TYPE_COMPLETE":
                return self._parse_trace((t.get("params") or {}).get("trace_file_ids"))
        raise PikPakError("转存超时")

    @staticmethod
    def _parse_trace(raw) -> dict:
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        try:
            return {str(k): str(v) for k, v in json.loads(raw or "{}").items()}
        except Exception:
            return {}

    def own_file_url(self, own_id: str) -> str:
        d = self._request(f"/drive/v1/files/{own_id}", "GET",
                          {"_magic": "2021", "usage": "FETCH",
                           "thumbnail_size": "SIZE_LARGE"})
        return self.pick_url(d)

    # ---------- 容量 / 删除（免费号只有几 GB，必须下完就删） ----------
    @staticmethod
    def _num(v) -> int:
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    def about(self) -> tuple:
        """返回 (总容量, 已用容量)，单位字节。"""
        d = self._request("/drive/v1/about", "GET")
        q = d.get("quota") or {}
        return self._num(q.get("limit")), self._num(q.get("usage"))

    def delete_files(self, ids: list) -> bool:
        """删掉自己网盘里的文件（只传系统转存进去的 id）。"""
        ids = [x for x in (ids or []) if x]
        if not ids:
            return False
        self._request("/drive/v1/files:batchDelete", "POST", None, {"ids": ids})
        return True
