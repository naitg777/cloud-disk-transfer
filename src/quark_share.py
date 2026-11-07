#!/usr/bin/env python3
"""
夸克网盘分享链接 → 直链 模块（两种模式）

模式一（默认）：**公开分享会话**
    不带任何账号 Cookie，只用一次性的 guest 会话向夸克要直链。
    好处：不消耗自己账号的配额、不会因为账号被限流而整体失败。
    公开分享的 stoken、文件列表、下载地址都可以通过公开接口读取。
    注意：直链必须带上同一次 guest 会话的 __sdid Cookie 才能下，
          所以 download_headers() 返回会话 Cookie 而不是账号 Cookie。

模式二（兜底）：**账号 Cookie**
    从 OpenList 的 SQLite 数据库读取 Quark Cookie，用于公开接口不可用时兜底。

用法：
    python3 quark_share.py <分享链接或含链接的文本> [--dir 目录名]
"""
import argparse
import json
import random
import re
import socket
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

import requests

# 服务器无 IPv6 出口，强制走 IPv4，避免 TLS 握手超时
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only

DB_PATH = "/data/openlist/data.db"
BASE = "https://pan.quark.cn/1/clouddrive"
# 公开会话走 PC 客户端的 API 域名：实测 pan.quark.cn 这条路径经常读超时，
# drive-pc 稳定得多（同一套接口，只是入口域名不同）
GUEST_BASE = "https://drive-pc.quark.cn/1/clouddrive"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 使用客户端风格的 UA 与参数，下载响应更稳定
CLIENT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) quark-cloud-drive/2.5.56 Chrome/100.0.4896.160 "
    "Electron/18.3.5.12-a038f7b798 Safari/537.36 Channel/pckk_other_ch"
)


class QuarkError(RuntimeError):
    """夸克 API 返回的业务错误"""


def load_cookie(db_path: str = DB_PATH) -> str:
    """从 OpenList 数据库读取 Quark 存储的 Cookie"""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT addition FROM x_storages WHERE driver='Quark' LIMIT 1")
        row = cur.fetchone()
        if not row:
            raise QuarkError("数据库中未找到 driver=Quark 的存储配置")
        addition = json.loads(row[0])
        cookie = addition.get("cookie", "")
        if not cookie:
            raise QuarkError("Quark 存储配置中 cookie 为空")
        return cookie
    finally:
        conn.close()


# 分页上限：单目录最多翻多少页（每页 200 条）
# 500 页 = 10 万条目/目录，正常资源远达不到；设上限只为防"接口异常时死循环"
MAX_PAGES_PER_DIR = 500


def _walk_share(fetch_page, pwd_id: str, stoken: str, pdir_fid: str = "0",
                path: str = "", depth: int = 0, limit: int | None = None,
                counter: list | None = None) -> list[dict]:
    """递归列分享内容（账号与公开会话共用这一份实现）。

    fetch_page(pwd_id, stoken, pdir_fid, page) -> list
    limit: 收集到这么多文件就停（None = 只受 MAX_PAGES_PER_DIR 约束）
    """
    if counter is None:
        counter = [0]
    if depth > 20:
        return []
    if limit is not None and counter[0] >= limit:
        return []

    result: list[dict] = []
    page = 1
    seen: set[str] = set()
    while True:
        items = fetch_page(pwd_id, stoken, pdir_fid, page)
        if not items:
            break
        new_items = 0
        for it in items:
            fid = it.get("fid")
            if not fid or fid in seen:
                continue
            seen.add(fid)
            new_items += 1
            name = it.get("file_name", "")
            sub_path = f"{path}/{name}" if path else name
            if it.get("dir"):
                result.extend(_walk_share(fetch_page, pwd_id, stoken, fid,
                                          sub_path, depth + 1, limit, counter))
                if limit is not None and counter[0] >= limit:
                    return result
            else:
                result.append({
                    "path": sub_path,
                    "name": name,
                    "size": it.get("size") or 0,
                    "fid": fid,
                    "pdir": pdir_fid,
                    "share_fid_token": it.get("share_fid_token", ""),
                })
                counter[0] += 1
                if limit is not None and counter[0] >= limit:
                    return result

        # 单页不足 200 或本页没有任何新条目时结束
        if len(items) < 200 or new_items == 0 or page >= MAX_PAGES_PER_DIR:
            break
        page += 1
    return result


class QuarkShare:
    def __init__(self, cookie: str | None = None):
        self.cookie = cookie or load_cookie()

    # ---------- 底层请求 ----------
    def _request(self, url: str, method: str = "GET", body: dict | None = None,
                 retries: int = 3, ua: str | None = None) -> dict:
        headers = {
            "Cookie": self.cookie,
            "User-Agent": ua or UA,
            "Referer": "https://pan.quark.cn/",
            "Origin": "https://pan.quark.cn",
            "Accept": "application/json, text/plain, */*",
        }
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_exc: Exception | None = None
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=25) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                # 夸克的业务错误同样走 4xx，需要解析 body 取出 message
                body_text = exc.read().decode("utf-8", "replace")
                # 网关/限流类 5xx、429 属于临时错误，退避后重试
                if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    last_exc = exc
                    time.sleep(2 ** attempt)
                    continue
                try:
                    payload = json.loads(body_text)
                except json.JSONDecodeError:
                    raise QuarkError(f"HTTP {exc.code}: {body_text[:120]}") from exc
                code = payload.get("code")
                msg = payload.get("message") or body_text[:120]
                if code == 41017:
                    raise QuarkError(
                        "这个分享是你自己创建的，夸克不允许转存自己的分享。"
                        "请换一个别人分享的链接。") from exc
                raise QuarkError(f"{msg}（code={code}）") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise QuarkError(f"网络错误（已重试 {retries} 次）：{exc}") from exc
        raise QuarkError(f"请求失败：{last_exc}")

    # ---------- 链接解析 ----------
    @staticmethod
    def parse_link(text: str) -> tuple[str, str]:
        """从任意文本中提取 pwd_id 与 passcode"""
        m = re.search(r"pan\.quark\.cn/s/([0-9a-zA-Z]+)", text)
        if not m:
            raise QuarkError("未找到夸克分享链接（应形如 https://pan.quark.cn/s/xxxx）")
        pwd_id = m.group(1)

        passcode = ""
        m2 = re.search(r"[?&#]pwd=([0-9a-zA-Z]{4})", text)
        if m2:
            passcode = m2.group(1)
        else:
            m3 = re.search(r"提取码[：:\s]*([0-9a-zA-Z]{4})", text)
            if m3:
                passcode = m3.group(1)
        return pwd_id, passcode

    # ---------- 业务接口 ----------
    def get_stoken(self, pwd_id: str, passcode: str = "") -> str:
        url = f"{BASE}/share/sharepage/token?pr=ucpro&fr=pc"
        d = self._request(url, "POST", {"pwd_id": pwd_id, "passcode": passcode})
        if d.get("code") != 0:
            msg = d.get("message") or "未知错误"
            if "密码" in str(msg) or d.get("code") == 41008:
                raise QuarkError(f"提取码错误或缺失：{msg}")
            raise QuarkError(f"获取 stoken 失败：{msg}")
        return d["data"]["stoken"]

    def list_share(self, pwd_id: str, stoken: str, pdir_fid: str = "0",
                   page: int = 1, size: int = 100) -> dict:
        qs = urllib.parse.urlencode({
            "pr": "ucpro", "fr": "pc",
            "pwd_id": pwd_id, "stoken": stoken,
            "pdir_fid": pdir_fid, "force": 0,
            "_page": page, "_size": size,
            "_fetch_banner": 1, "_fetch_share": 1, "_fetch_total": 1,
            "_sort": "file_type:asc,updated_at:desc",
        })
        d = self._request(f"{BASE}/share/sharepage/detail?{qs}")
        if d.get("code") != 0:
            raise QuarkError(f"列出分享内容失败：{d.get('message')}")
        return d["data"]

    def save(self, pwd_id: str, stoken: str, fid_list: list[str],
             fid_token_list: list[str], to_pdir_fid: str = "0") -> str:
        qs = urllib.parse.urlencode({
            "pr": "ucpro",
            "fr": "pc",
            "uc_param_str": "",
            "app": "clouddrive",
            "__dt": int(random.uniform(1, 5) * 60 * 1000),
            "__t": datetime.now().timestamp(),
        })
        url = f"{BASE}/share/sharepage/save?{qs}"
        body = {
            "fid_list": fid_list,
            "fid_token_list": fid_token_list,
            "to_pdir_fid": to_pdir_fid,
            "pwd_id": pwd_id,
            "stoken": stoken,
            "pdir_fid": "0",
            "scene": "link",
        }
        d = self._request(url, "POST", body)
        if d.get("code") != 0:
            raise QuarkError(f"提交转存任务失败：{d.get('message')}")
        return d["data"]["task_id"]

    def wait_task(self, task_id: str, timeout: int = 300, interval: float = 2.0) -> None:
        """轮询任务直到完成。status: 0=等待 1=进行 2=完成 3=失败"""
        self.wait_task_fids(task_id, timeout, interval)

    def wait_task_fids(self, task_id: str, timeout: int = 300,
                       interval: float = 2.0) -> list[str]:
        """轮询转存任务直到完成，返回转存后自己网盘里的新 fid 列表。

        夸克的 save_as_top_fids 与提交时的 fid_list **顺序一一对应**，
        所以调用方可以直接按下标映射「原 fid → 新 fid」。
        """
        qs = urllib.parse.urlencode({
            "pr": "ucpro", "fr": "pc",
            "task_id": task_id, "retry_index": 0,
        })
        url = f"{BASE}/task?{qs}"
        deadline = time.time() + timeout
        while time.time() < deadline:
            d = self._request(url)
            data = d.get("data") or {}
            status = data.get("status")
            if status == 2:
                save_as = data.get("save_as") or {}
                fids = (save_as.get("save_as_top_fids")
                        or save_as.get("save_as_select_top_fids") or [])
                return [str(x) for x in fids]
            if status == 3:
                raise QuarkError(f"转存任务失败：{data.get('message')}")
            time.sleep(interval)
        raise QuarkError(f"转存任务超时（{timeout}s）")

    def own_direct_url(self, fid: str) -> str:
        """自己网盘的下载直链（不是分享的）。

        实测：分享直链被 CDN 拒（HTTP 412）时，自己网盘的直链仍然可用，
        所以「转存 → 从自己网盘下」是可靠兜底。
        """
        qs = urllib.parse.urlencode({
            "pr": "ucpro", "fr": "pc", "sys": "win32", "ve": "2.5.56",
            "ut": "", "guid": "",
        })
        d = self._request(f"{BASE}/file/download?{qs}", "POST", {"fids": [fid]})
        if d.get("code") != 0:
            raise QuarkError(f"取自己网盘直链失败：{d.get('message')}")
        data = d.get("data") or []
        if not data or not data[0].get("download_url"):
            raise QuarkError("自己网盘没返回下载地址")
        return data[0]["download_url"]

    def member_info(self) -> dict:
        """账号容量 / 会员信息（用于判断「转存桥接」还有没有落脚空间）。"""
        d = self._request(
            "https://drive-pc.quark.cn/1/clouddrive/member?pr=ucpro&fr=pc")
        return d.get("data") or {}

    def quota(self) -> tuple[int, int]:
        """返回 (总容量, 已用容量)，单位字节。拿不到就返回 (0, 0)。"""
        data = self.member_info()
        return int(data.get("total_capacity") or 0), int(data.get("use_capacity") or 0)

    def ensure_dir(self, name: str, pdir_fid: str = "0") -> str:
        """在自己网盘根目录下取（或建）一个目录，返回 fid。"""
        for it in self.list_dir(pdir_fid):
            if it.get("dir") and it.get("file_name") == name:
                return it["fid"]
        return self.mkdir(name, pdir_fid)

    def list_dir(self, pdir_fid: str = "0", size: int = 200) -> list[dict]:
        """列出自己网盘的某个目录"""
        qs = urllib.parse.urlencode({
            "pr": "ucpro", "fr": "pc",
            "pdir_fid": pdir_fid, "force": 0,
            "_page": 1, "_size": size,
            "_fetch_total": 1,
            "_sort": "file_type:asc,updated_at:desc",
        })
        d = self._request(f"{BASE}/file/sort?{qs}")
        if d.get("code") != 0:
            raise QuarkError(f"列出目录失败：{d.get('message')}")
        return (d.get("data") or {}).get("list", [])

    def find_dir(self, name: str, pdir_fid: str = "0") -> str | None:
        """在指定目录下按名称查找子目录，返回 fid"""
        for it in self.list_dir(pdir_fid):
            if it.get("dir") and it.get("file_name") == name:
                return it.get("fid")
        return None

    def mkdir(self, name: str, pdir_fid: str = "0") -> str:
        """创建目录，返回新目录 fid；若已存在则返回现有 fid"""
        existing = self.find_dir(name, pdir_fid)
        if existing:
            return existing
        url = f"{BASE}/file?pr=ucpro&fr=pc"
        body = {
            "pdir_fid": pdir_fid,
            "file_name": name,
            "dir_path": "",
            "dir_init_lock": False,
        }
        d = self._request(url, "POST", body)
        if d.get("code") != 0:
            raise QuarkError(f"创建目录失败：{d.get('message')}")
        return d["data"]["fid"]

    def delete(self, fids: list[str]) -> None:
        """删除自己网盘中的文件（按 fid 列表）"""
        if not fids:
            return
        url = f"{BASE}/file/delete?pr=ucpro&fr=pc"
        body = {
            "action_type": 2,
            "filelist": fids,
            "exclude_fids": [],
        }
        d = self._request(url, "POST", body)
        if d.get("code") != 0:
            raise QuarkError(f"删除失败：{d.get('message')}")

    # ---------- 不转存直连（推荐路径）----------
    def _fetch_page(self, pwd_id: str, stoken: str, pdir_fid: str, page: int) -> list:
        qs = urllib.parse.urlencode({
            "pr": "ucpro", "fr": "pc",
            "pwd_id": pwd_id, "stoken": stoken,
            "pdir_fid": pdir_fid, "force": 0,
            "_page": page, "_size": 200,
            "_fetch_banner": 1, "_fetch_share": 1, "_fetch_total": 1,
            "_sort": "file_type:asc,updated_at:desc",
        })
        d = self._request(f"{BASE}/share/sharepage/detail?{qs}")
        if d.get("code") != 0:
            raise QuarkError(f"列出分享内容失败：{d.get('message')}")
        return (d.get("data") or {}).get("list") or []

    def walk_share(self, pwd_id: str, stoken: str, pdir_fid: str = "0",
                   path: str = "", depth: int = 0, limit: int | None = None) -> list[dict]:
        """
        递归列出分享内的所有文件，返回
        [{path, name, size, fid, share_fid_token}]
        只读元数据，不下载、不转存。

        limit: 最多收集多少个文件（None = 不限制，仅受分页上限约束）。
               调用方传入"上限+1"就能提前停止，避免超大分享白白翻几十页。
        """
        return _walk_share(
            self._fetch_page, pwd_id, stoken, pdir_fid, path, depth, limit)

    def get_direct_url(self, fid: str, share_fid_token: str,
                       pwd_id: str, stoken: str) -> str:
        """
        不转存，直接从分享获取下载直链。
        使用客户端风格的 UA 与参数请求直链。
        """
        qs = urllib.parse.urlencode({
            "pr": "ucpro", "fr": "pc",
            "sys": "win32", "ve": "2.5.56",
            "ut": "", "guid": "",
        })
        body = {
            "fids": [fid],
            "fids_token": [share_fid_token],
            "pwd_id": pwd_id,
            "stoken": stoken,
            "speedup_session": "",
            "token": "",
        }
        d = self._request(f"{BASE}/file/download?{qs}", "POST", body, ua=CLIENT_UA)
        if d.get("code") != 0:
            raise QuarkError(f"获取直链失败：{d.get('message')}")
        data = d.get("data") or []
        if not data or not data[0].get("download_url"):
            raise QuarkError("接口未返回 download_url")
        return data[0]["download_url"]

    def download_headers(self) -> dict[str, str]:
        """下载直链所需的请求头。CDN 会校验请求头，缺 Cookie 会返回 412"""
        return {
            "Cookie": self.cookie,
            "User-Agent": CLIENT_UA,
            "Referer": "https://pan.quark.cn/",
        }


class QuarkGuest:
    """公开分享访问：不需要账号信息即可读取分享内容。

    夸克的分享接口在未登录状态下可用：
      POST /share/sharepage/token   拿 stoken
      GET  /share/sharepage/detail  列文件
      POST /file/download           换直链
    但返回的直链绑定**同一次 guest 会话**（Cookie 里的 __sdid），
    所以下载时必须带上这个会话 Cookie，否则 CDN 回 412。

    直链域名是 dl-guest-xxx.drive.quark.cn，和账号版不是同一个域名，
    也不走 /etc/hosts 里为账号版固定的节点，等于绕开了账号侧的限流。
    """

    parse_link = staticmethod(QuarkShare.parse_link)

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Referer": "https://pan.quark.cn/",
            "Origin": "https://pan.quark.cn",
            "Accept": "application/json, text/plain, */*",
        })
        self.cookie = ""

    # ---------- 内部 ----------
    def _json(self, resp) -> dict:
        if resp.status_code >= 400:
            raise QuarkError(f"HTTP {resp.status_code}: {(resp.text or '')[:120]}")
        try:
            d = resp.json()
        except Exception:
            raise QuarkError("返回内容不是 JSON")
        if d.get("code") not in (0, None):
            raise QuarkError(d.get("message") or f"code={d.get('code')}")
        return d

    def _sync_cookie(self) -> None:
        self.cookie = "; ".join(f"{c.name}={c.value}" for c in self.s.cookies)

    # ---------- 业务 ----------
    def get_stoken(self, pwd_id: str, passcode: str = "") -> str:
        d = self._json(self.s.post(
            f"{GUEST_BASE}/share/sharepage/token", params={"pr": "ucpro", "fr": "pc"},
            json={"pwd_id": pwd_id, "passcode": passcode}, timeout=20))
        st = (d.get("data") or {}).get("stoken")
        if not st:
            raise QuarkError("获取分享信息失败（这个分享可能需要登录才能查看）")
        self._sync_cookie()
        return st

    def _fetch_page(self, pwd_id: str, stoken: str, pdir_fid: str, page: int) -> list:
        params = {
            "pr": "ucpro", "fr": "pc", "pwd_id": pwd_id, "stoken": stoken,
            "pdir_fid": pdir_fid, "force": 0, "_page": page, "_size": 200,
            "_fetch_banner": 1, "_fetch_share": 1, "_fetch_total": 1,
            "_sort": "file_type:asc,updated_at:desc",
        }
        d = self._json(self.s.get(f"{GUEST_BASE}/share/sharepage/detail",
                                  params=params, timeout=25))
        return (d.get("data") or {}).get("list") or []

    def walk_share(self, pwd_id: str, stoken: str, pdir_fid: str = "0",
                   path: str = "", depth: int = 0, limit: int | None = None) -> list:
        return _walk_share(
            self._fetch_page, pwd_id, stoken, pdir_fid, path, depth, limit)

    def get_direct_url(self, fid: str, share_fid_token: str,
                       pwd_id: str, stoken: str) -> str:
        params = {"pr": "ucpro", "fr": "pc", "sys": "win32", "ve": "2.5.56",
                  "ut": "", "guid": ""}
        d = self._json(self.s.post(
            f"{GUEST_BASE}/file/download", params=params,
            json={"fids": [fid], "fids_token": [share_fid_token],
                  "pwd_id": pwd_id, "stoken": stoken, "pdir_fid": "0",
                  "speedup_session": "", "token": ""}, timeout=25))
        data = d.get("data") or []
        if not data or not data[0].get("download_url"):
            raise QuarkError("公开接口没返回下载地址")
        self._sync_cookie()
        return data[0]["download_url"]

    def download_headers(self) -> dict[str, str]:
        """必须带 guest 会话的 __sdid，否则 CDN 回 412。"""
        self._sync_cookie()
        return {"Cookie": self.cookie, "User-Agent": UA,
                "Referer": "https://pan.quark.cn/"}


def transfer(link_text: str, target_dir_name: str | None = None,
             verbose: bool = True) -> dict:
    """
    执行完整转存流程，返回结果摘要。

    返回:
        {
            "pwd_id": str,
            "passcode": str,
            "items": [{"name": str, "dir": bool, "size": int}],
            "target_fid": str,
            "task_id": str,
        }
    """
    pwd_id, passcode = QuarkShare.parse_link(link_text)
    q = QuarkShare()

    if verbose:
        print(f"[1/5] 解析链接  pwd_id={pwd_id}  passcode={passcode or '(无)'}")

    stoken = q.get_stoken(pwd_id, passcode)
    if verbose:
        print(f"[2/5] stoken 获取成功")

    data = q.list_share(pwd_id, stoken)
    items = data.get("list") or []
    if not items:
        raise QuarkError("分享内容为空")
    if verbose:
        print(f"[3/5] 分享内共 {len(items)} 项：")
        for it in items[:20]:
            kind = "DIR " if it.get("dir") else "FILE"
            size_mb = (it.get("size") or 0) / 1048576
            print(f"       [{kind}] {it.get('file_name')}  {size_mb:.2f} MB")

    target_fid = "0"
    if target_dir_name:
        target_fid = q.ensure_dir(target_dir_name)
        if verbose:
            print(f"[4/5] 目标目录 {target_dir_name} fid={target_fid}")
    else:
        if verbose:
            print(f"[4/5] 转存到根目录 fid=0")

    fid_list = [it["fid"] for it in items]
    token_list = [it.get("share_fid_token", "") for it in items]
    task_id = q.save(pwd_id, stoken, fid_list, token_list, target_fid)
    if verbose:
        print(f"[5/5] 转存任务已提交 task_id={task_id}，等待完成…")

    q.wait_task(task_id)
    if verbose:
        print("      转存完成")

    return {
        "pwd_id": pwd_id,
        "passcode": passcode,
        "items": [
            {"name": it.get("file_name"), "dir": bool(it.get("dir")),
             "size": it.get("size") or 0}
            for it in items
        ],
        "target_fid": target_fid,
        "task_id": task_id,
    }


def main():
    parser = argparse.ArgumentParser(description="夸克分享链接转存")
    parser.add_argument("link", help="分享链接或包含链接的文本")
    parser.add_argument("--dir", dest="dir_name", default=None,
                        help="转存目标目录名（不存在会自动创建）；省略则转存到根目录")
    args = parser.parse_args()

    try:
        result = transfer(args.link, args.dir_name)
        print()
        print("=== 结果 ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except QuarkError as exc:
        print(f"\n[错误] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
