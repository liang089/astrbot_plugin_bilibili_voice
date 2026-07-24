import asyncio
import html
import json
import re
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import httpx
import yt_dlp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Record
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.message.message_event_result import MessageChain


BILI_QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILI_QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
BILI_SEARCH = "https://api.bilibili.com/x/web-interface/search/type"
BILI_NAV = "https://api.bilibili.com/x/web-interface/nav"
BILI_VIDEO_INFO = "https://api.bilibili.com/x/web-interface/view"
BV_PATTERN = re.compile(r"^[Bb][Vv][0-9A-Za-z]{10,}$")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
BUVTD3 = str(uuid.uuid4()).replace("-", "")[:16] + "infoc"
BILI_BASE_COOKIES = {"buvid3": BUVTD3, "buvid4": str(uuid.uuid4()).replace("-", "")[:24] + "infoc"}


@register("astrbot_plugin_bilibili_voice", "sB123", "Bilibili 搜索下载转 QQ 语音", "v2.0.0")
class BilibiliVoicePlugin(Star):

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = config or {}
        self.download_dir = Path(str(cfg.get("download_dir", "data/bilibili_voice") or "data/bilibili_voice")).resolve()
        self.max_duration = int(cfg.get("max_duration_seconds", 900) or 0)
        self.search_limit = max(1, min(10, int(cfg.get("search_limit", 5) or 5)))

        self.data_dir = self.download_dir
        self.cookie_file = self.data_dir / "bilibili_cookies.txt"
        self.state_file = self.data_dir / "login_state.json"
        self.last_search: dict[str, list[dict[str, Any]]] = {}
        self.qrcode_key = ""
        self.qrcode_url = ""
        self.login_status = "未登录"
        # Register Pages Web APIs
        PREFIX = "astrbot_plugin_bilibili_voice"
        context.register_web_api(f"/{PREFIX}/qrcode", self.api_qrcode, ["GET"], "获取登录二维码")
        context.register_web_api(f"/{PREFIX}/qrcode/refresh", self.api_qrcode_refresh, ["GET"], "刷新登录二维码")
        context.register_web_api(f"/{PREFIX}/status", self.api_status, ["GET"], "查看登录状态")
        context.register_web_api(f"/{PREFIX}/search", self.api_search, ["GET"], "搜索视频")
        context.register_web_api(f"/{PREFIX}/download", self.api_download, ["GET"], "下载转MP3")

    async def initialize(self):
        self.data_dir = self.data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        await self._load_state()

    async def terminate(self):
        pass

    # ---- Chat commands ----

    @filter.command("bili_login", alias={"b站登录", "bili扫码"})
    async def bili_login(self, event: AstrMessageEvent):
        """获取 Bilibili 扫码登录二维码。"""
        await self._ensure_qrcode(force=True)
        qr_img = "https://api.qrserver.com/v1/create-qr-code/?size=360x360&data=" + urllib.parse.quote(self.qrcode_url)
        yield event.image_result(qr_img)
        yield event.plain_result("请用哔哩哔哩APP扫码登录，确认后发送 /bili_status 查看状态。")

    @filter.command("bili_refresh", alias={"刷新二维码", "bili刷新"})
    async def bili_refresh(self, event: AstrMessageEvent):
        """刷新二维码。"""
        await self._ensure_qrcode(force=True)
        qr_img = "https://api.qrserver.com/v1/create-qr-code/?size=360x360&data=" + urllib.parse.quote(self.qrcode_url)
        yield event.image_result(qr_img)
        yield event.plain_result("二维码已刷新，请用哔哩哔哩APP扫码，确认后发送 /bili_status 查看状态。")

    @filter.command("bili_status", alias={"b站状态"})
    async def bili_status(self, event: AstrMessageEvent):
        """查看 Bilibili 登录状态。"""
        if self.qrcode_key:
            await self._poll_login()
        status = await self._check_login_status()
        yield event.plain_result(status)

    @filter.command("bili_search", alias={"b站搜索"})
    async def bili_search(self, event: AstrMessageEvent):
        """搜索 Bilibili 视频：/bili_search 关键词"""
        keyword = self._command_body(event.get_message_str(), ["/bili_search", "bili_search", "/b站搜索", "b站搜索"]).strip()
        if not keyword:
            yield event.plain_result("用法：/bili_search 关键词")
            return
        try:
            results = await self._search(keyword)
        except Exception as exc:
            logger.error(f"Bilibili search failed: {exc}")
            yield event.plain_result(f"搜索失败：{exc}")
            return
        if not results:
            yield event.plain_result("没有搜到结果。")
            return
        self.last_search[str(event.session)] = results
        lines = ["搜索结果（共 {} 条）：".format(len(results))]
        for idx, item in enumerate(results, 1):
            title = item.get("title", "无标题")
            author = item.get("author", "未知UP")
            duration = item.get("duration", "")
            bvid = item.get("bvid", "")
            lines.append(f"编号 {idx}. {title}")
            pn = item.get("page")
            page_tag = f" [P{pn}]" if pn else ""
            lines.append(f"   UP {author} / {duration} / {bvid}{page_tag}")
        lines.append("下载：/bili_down 编号（如 /bili_down 1）")
        lines.append("或直接 /bili_down BV号/链接")
        yield event.plain_result("\n".join(lines))

    @filter.command("bili_down", alias={"b站下载"})
    async def bili_down(self, event: AstrMessageEvent):
        """下载搜索结果或链接并转成 QQ 语音：/bili_down 1 或 /bili_down BV..."""
        arg = self._command_body(event.get_message_str(), ["/bili_down", "bili_down", "/b站下载", "b站下载"]).strip()
        if not arg:
            yield event.plain_result("用法：\n/bili_down 编号（引用搜索结果）\n/bili_down BV号\n/bili_down https://www.bilibili.com/video/BVxxx")
            return
        url, page = self._resolve_download_url(str(event.session), arg)
        p_msg = f" (P{page})" if page > 0 else ""
        yield event.plain_result(f"开始下载并转换 MP3{p_msg}，请稍等...")
        try:
            audio_path = await asyncio.to_thread(self._download_mp3, url, page)
        except Exception as exc:
            logger.error(f"Bilibili download failed: {exc}")
            yield event.plain_result(f"下载失败：{exc}")
            return
        ok = await self._send_record(event, audio_path)
        if not ok:
            yield event.plain_result(f"语音发送失败，文件已保存在：{audio_path}")

    # ---- Internal methods ----

    async def _ensure_qrcode(self, force: bool = False):
        if self.qrcode_key and self.qrcode_url and not force:
            return
        async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=15) as client:
            resp = await client.get(BILI_QR_GENERATE)
            resp.raise_for_status()
            data = resp.json().get("data") or {}
            self.qrcode_key = str(data.get("qrcode_key") or "")
            self.qrcode_url = str(data.get("url") or "")
            self.login_status = "请使用 Bilibili 手机端扫码登录"

    async def _poll_login(self):
        async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=15) as client:
            resp = await client.get(BILI_QR_POLL, params={"qrcode_key": self.qrcode_key})
            resp.raise_for_status()
            payload = resp.json().get("data") or {}
            code = int(payload.get("code", -1))
            if code == 0:
                self._save_cookies(resp.cookies)
                self.qrcode_key = ""
                self.qrcode_url = ""
                self.login_status = "已登录"
                await self._save_state()
            elif code == 86038:
                self.login_status = "二维码已过期，请发送 /bili_refresh 刷新"
            elif code == 86090:
                self.login_status = "已扫码，请在手机上确认登录"
            else:
                msg = str(payload.get("message") or "")
                self.login_status = msg or "等待扫码"

    async def _check_login_status(self) -> str:
        cookies = self._load_cookie_dict()
        if not cookies:
            return "未登录。发送 /bili_login 获取二维码扫码登录。"
        async with httpx.AsyncClient(headers={"User-Agent": UA}, cookies=cookies, timeout=15) as client:
            resp = await client.get(BILI_NAV)
            if resp.status_code != 200:
                return "登录状态检查失败。"
            data = resp.json()
            if data.get("code") == 0 and data.get("data", {}).get("isLogin"):
                name = data.get("data", {}).get("uname") or "Bilibili 用户"
                return f"已登录：{name}"
            return "登录已失效。发送 /bili_login 重新扫码。"

    async def _search(self, keyword: str) -> list[dict[str, Any]]:
        cookies = self._load_cookie_dict()
        if not cookies:
            cookies = dict(BILI_BASE_COOKIES)

        # BV number lookup
        if BV_PATTERN.match(keyword):
            bvid = keyword.upper()
            async with httpx.AsyncClient(headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"}, cookies=cookies, timeout=20) as client:
                resp = await client.get(BILI_VIDEO_INFO, params={"bvid": bvid})
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("code") == 0 and data.get("data"):
                        v = data["data"]
                        video_count = int(v.get("videos", 1) or 1)
                        title = str(v.get("title", keyword))
                        pages = v.get("pages", [])
                        if video_count > 1 and pages:
                            results = []
                            for pi, page in enumerate(pages, 1):
                                pdur = page.get("duration", 0)
                                pm, ps = divmod(pdur, 60)
                                ph, pm = divmod(pm, 60)
                                pdur_str = f"{ph}:{pm:02d}:{ps:02d}" if ph else f"{pm}:{ps:02d}"
                                ptitle = str(page.get("part", f"P{pi}"))
                                results.append({
                                    "title": f"{title} - {ptitle}",
                                    "author": str(v.get("owner", {}).get("name", "未知UP")),
                                    "duration": pdur_str,
                                    "bvid": bvid,
                                    "url": f"https://www.bilibili.com/video/{bvid}?p={pi}",
                                    "page": pi,
                                })
                            return results
                        else:
                            total_duration = v.get("duration", 0)
                            mm, ss = divmod(total_duration, 60)
                            hh, mm = divmod(mm, 60)
                            dur_str = f"{hh}:{mm:02d}:{ss:02d}" if hh else f"{mm}:{ss:02d}"
                            return [{"title": title, "author": str(v.get("owner", {}).get("name", "未知UP")), "duration": dur_str, "bvid": bvid, "url": f"https://www.bilibili.com/video/{bvid}", "page": 1}]
            return []

        # Normal keyword search
        params = {"search_type": "video", "keyword": keyword, "page": 1}
        async with httpx.AsyncClient(headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/", "Origin": "https://search.bilibili.com"}, cookies=cookies, timeout=20) as client:
            resp = await client.get(BILI_SEARCH, params=params)
            resp.raise_for_status()
            data = resp.json().get("data") or {}
        items = []
        for raw in (data.get("result") or [])[: self.search_limit]:
            title = self._strip_tags(str(raw.get("title") or "无标题"))
            bvid = str(raw.get("bvid") or "")
            arcurl = str(raw.get("arcurl") or "")
            if not bvid and not arcurl:
                continue
            items.append({"title": title, "author": str(raw.get("author") or "未知UP"), "duration": str(raw.get("duration") or ""), "bvid": bvid, "url": arcurl or f"https://www.bilibili.com/video/{bvid}"})
        return items

    def _download_mp3(self, url: str, page: int = 0) -> str:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        outtmpl = str(self.data_dir / "%(title).80s-%(id)s.%(ext)s")
        opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "noplaylist": page == 0,
            "quiet": True,
            "no_warnings": True,
            "extractor_args": {"bilibili": {"query": ["platform=web", "high_quality=False"]}},
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav", "preferredquality": "0"}],
            "postprocessor_args": {"ffmpeg": ["-ar", "44100", "-ac", "1"]},
        }
        if page > 0:
            opts["playlist_items"] = str(page)
        if self.cookie_file.exists():
            opts["cookiefile"] = str(self.cookie_file)
        if self.max_duration > 0:
            def match_filter(info, *, incomplete):
                d = int(info.get("duration") or 0)
                if d and d > self.max_duration:
                    return f"视频过长：{d} 秒，限制 {self.max_duration} 秒"
                return None
            opts["match_filter"] = match_filter
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
        mp3_path = str(Path(filename).with_suffix(".wav"))
        if not Path(mp3_path).exists():
            matches = sorted(self.data_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not matches:
                raise RuntimeError("MP3 文件未生成，请确认已安装 ffmpeg")
            mp3_path = str(matches[0])
        return str(Path(mp3_path).resolve())

    async def _send_record(self, event: AstrMessageEvent, file_path: str) -> bool:
        """Send audio - try OneBot record via bot.call_action, then fallbacks."""
        bot = getattr(event, "bot", None)
        gid = str(event.get_group_id() or "").strip()
        uid = str(event.get_sender_id() or "").strip()
        fname = Path(file_path).name

        if bot and (gid.isdigit() or uid.isdigit()):
            tgt = int(gid) if gid.isdigit() else int(uid)
            act = "send_group_msg" if gid.isdigit() else "send_private_msg"
            k = "group_id" if gid.isdigit() else "user_id"
            file_uri = Path(file_path).resolve().as_uri()

            # 1) Record (voice) via bot.call_action - try base64 first, then file:///
            import base64 as b64m
            try:
                with open(file_path, "rb") as f:
                    raw = f.read()
                b64 = "base64://" + b64m.b64encode(raw).decode("ascii")
                seg = {"type": "record", "data": {"file": b64}}
                await bot.call_action(act, **{k: tgt}, message=[seg])
                logger.info("Record base64 sent OK")
                return True
            except Exception as exc:
                logger.warning("Record base64 failed: " + str(exc))
            try:
                seg = {"type": "record", "data": {"file": file_uri}}
                await bot.call_action(act, **{k: tgt}, message=[seg])
                logger.info("Record file:/// sent OK")
                return True
            except Exception as exc:
                logger.warning("Record file:/// failed: " + str(exc))

            # 2) File via bot.call_action with base64
            try:
                import base64 as b64m
                with open(file_path, "rb") as f:
                    raw = f.read()
                b64 = "base64://" + b64m.b64encode(raw).decode("ascii")
                seg = {"type": "file", "data": {"file": b64, "name": fname}}
                await bot.call_action(act, **{k: tgt}, message=[seg])
                logger.info("base64 file sent OK")
                return True
            except Exception as exc:
                logger.warning("base64 file failed: " + str(exc))

            # 3) Record via AstrBot component
            try:
                await event.send(MessageChain([Record.fromFileSystem(file_path)]))
                return True
            except Exception as exc:
                logger.warning("Record.fromFileSystem failed: " + str(exc))

            # 4) File via AstrBot component
            try:
                await event.send(MessageChain([File(name=fname, file=file_uri)]))
                return True
            except Exception as exc:
                logger.warning("File component failed: " + str(exc))

        logger.error("All send methods exhausted: " + file_path)
        return False
    def _resolve_download_url(self, session: str, arg: str):
        """Return (url, page_number). page=0 means auto."""
        if arg.isdigit():
            items = self.last_search.get(session) or []
            idx = int(arg) - 1
            if idx < 0 or idx >= len(items):
                raise ValueError("编号不存在或搜索已过期，请重新 /bili_search 搜索")
            item = items[idx]
            page = int(item.get("page", 0) or 0)
            return (str(item["url"]), page)
        if arg.startswith("http://") or arg.startswith("https://"):
            return (arg, 0)
        return (f"https://www.bilibili.com/video/{arg}", 0)

    def _save_cookies(self, cookies: httpx.Cookies):
        lines = ["# Netscape HTTP Cookie File"]
        for cookie in cookies.jar:
            domain = cookie.domain or ".bilibili.com"
            d = "TRUE" if domain.startswith(".") else "FALSE"
            secure = "TRUE" if cookie.secure else "FALSE"
            expires = str(int(cookie.expires or (time.time() + 31536000)))
            lines.append("\t".join([domain, d, cookie.path or "/", secure, expires, cookie.name, cookie.value]))
        self.cookie_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _load_cookie_dict(self) -> dict[str, str]:
        if not self.cookie_file.exists():
            return {}
        cookies = {}
        for line in self.cookie_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies[parts[5]] = parts[6]
        return cookies

    async def _load_state(self):
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.login_status = str(data.get("login_status") or self.login_status)
        except Exception:
            pass

    async def _save_state(self):
        self.state_file.write_text(json.dumps({"login_status": self.login_status}, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- Pages Web API handlers ----

    async def api_qrcode(self):
        await self._ensure_qrcode(force=True)
        return json_response({"qrcode_url": self.qrcode_url, "qrcode_key": self.qrcode_key})

    async def api_qrcode_refresh(self):
        self.qrcode_key = ""
        self.qrcode_url = ""
        await self._ensure_qrcode(force=True)
        return json_response({"qrcode_url": self.qrcode_url, "qrcode_key": self.qrcode_key})

    async def api_status(self):
        if self.qrcode_key:
            await self._poll_login()
        cookies = self._load_cookie_dict()
        if not cookies:
            return json_response({"logged_in": False, "nickname": "", "username": request.username})
        async with httpx.AsyncClient(headers={"User-Agent": UA}, cookies=cookies, timeout=15) as client:
            resp = await client.get(BILI_NAV)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0 and data.get("data", {}).get("isLogin"):
                    name = data.get("data", {}).get("uname") or ""
                    return json_response({"logged_in": True, "nickname": name, "username": request.username})
        return json_response({"logged_in": False, "nickname": "", "username": request.username})

    async def api_search(self):
        keyword = request.query.get("keyword", "")
        if not keyword:
            return error_response("keyword is required")
        try:
            results = await self._search(keyword)
        except Exception as e:
            return error_response(str(e))
        return json_response({"results": results})

    async def api_download(self):
        url = request.query.get("url", "")
        page = int(request.query.get("page", "0") or "0")
        if not url:
            return error_response("url is required")
        try:
            audio_path = await asyncio.to_thread(self._download_mp3, url, page)
            return json_response({"file": audio_path, "success": True})
        except Exception as e:
            return error_response(str(e))

    @staticmethod
    def _strip_tags(text: str) -> str:
        return html.unescape(text.replace('<em class="keyword">', "").replace("</em>", ""))

    @staticmethod
    def _command_body(message: str, names: list[str]) -> str:
        text = str(message or "").strip()
        for name in names:
            if text.startswith(name):
                return text[len(name):].strip()
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""
