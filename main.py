import asyncio
import base64
import html
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import httpx
import yt_dlp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Plain, Record
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.message.message_event_result import MessageChain

try:  # file_response was added together with astrbot.api.web (AstrBot >= 4.26)
    from astrbot.api.web import file_response
except ImportError:  # pragma: no cover - only for very old AstrBot builds
    file_response = None


PLUGIN_NAME = "astrbot_plugin_bilibili_voice"

BILI_QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILI_QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
BILI_SEARCH = "https://api.bilibili.com/x/web-interface/search/type"
BILI_NAV = "https://api.bilibili.com/x/web-interface/nav"
BILI_VIDEO_INFO = "https://api.bilibili.com/x/web-interface/view"
BILI_REFERER = "https://www.bilibili.com/"

BV_PATTERN = re.compile(r"^[Bb][Vv][0-9A-Za-z]{10}$")
BV_IN_TEXT = re.compile(r"[Bb][Vv][0-9A-Za-z]{10}")
PAGE_IN_TEXT = re.compile(r"[?&]p=(\d+)")
TAG_PATTERN = re.compile(r"<[^>]+>")
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)
BUVTD3 = str(uuid.uuid4()).replace("-", "")[:16] + "infoc"
BILI_BASE_COOKIES = {
    "buvid3": BUVTD3,
    "buvid4": str(uuid.uuid4()).replace("-", "")[:24] + "infoc",
}
HTTP_HEADERS = {"User-Agent": UA, "Referer": BILI_REFERER}
# Bilibili's Tencent EdgeOne CDN (upos-sz-mirrorcosov.bilivideo.com) returns
# HTTP 567 unless media requests carry a Range header. yt-dlp issue #17847.
DOWNLOAD_HEADERS = {
    **HTTP_HEADERS,
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Range": "bytes=0-",
}
COOKIE_TTL = 3600
MAX_DOWNLOAD_TOKENS = 64
# QQ voice uploads are unreliable for very large WAV files; send those as a
# normal file attachment instead of a voice message.
VOICE_MAX_BYTES = 20 * 1024 * 1024
AUDIO_CONTENT_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".webm": "audio/webm",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
}


@register(
    "astrbot_plugin_bilibili_voice",
    "sB123",
    "Bilibili 搜索下载转 QQ 语音",
    "v2.1.1",
)
class BilibiliVoicePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = config or {}

        raw_dir = str(cfg.get("download_dir") or "data/bilibili_voice").strip()
        download_dir = Path(raw_dir).expanduser()
        if not download_dir.is_absolute():
            download_dir = Path.cwd() / download_dir
        self.download_dir = download_dir.resolve()

        self.max_duration = max(0, int(cfg.get("max_duration_seconds", 1800) or 0))
        self.search_limit = max(1, min(20, int(cfg.get("search_limit", 5) or 5)))
        self.ffmpeg_path = str(cfg.get("ffmpeg_path") or "").strip()
        self.proxy = str(cfg.get("proxy") or "").strip()
        self.send_as_voice = bool(cfg.get("send_as_voice", True))

        self.data_dir = self.download_dir
        self.cookie_file = self.data_dir / "bilibili_cookies.txt"
        self.anon_cookie_file = self.data_dir / "bilibili_anon_cookies.txt"
        self.state_file = self.data_dir / "login_state.json"

        self.last_search: dict[str, list[dict[str, Any]]] = {}
        self.qrcode_key = ""
        self.qrcode_url = ""
        self.login_status = "未登录"

        # token -> {"path": str, "expires": float}
        self._download_tokens: dict[str, dict[str, Any]] = {}
        self._download_locks: dict[str, asyncio.Lock] = {}

        # Pages / Views Web APIs
        context.register_web_api(
            f"/{PLUGIN_NAME}/qrcode", self.api_qrcode, ["GET"], "获取登录二维码"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/qrcode/refresh",
            self.api_qrcode_refresh,
            ["GET"],
            "刷新登录二维码",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/status", self.api_status, ["GET"], "查看登录状态"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/search", self.api_search, ["GET"], "搜索视频"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/download", self.api_download, ["GET"], "下载并转码"
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/download/file",
            self.api_download_file,
            ["GET"],
            "下载音频文件",
        )

    async def initialize(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_anon_cookie_file()
        await self._load_state()

    async def terminate(self):
        self._download_tokens.clear()

    # ------------------------------------------------------------------
    # Chat commands
    # ------------------------------------------------------------------

    @filter.command("bili_help", alias={"b站帮助", "bili帮助"})
    async def bili_help(self, event: AstrMessageEvent):
        """查看使用帮助。"""
        text = (
            "B站语音下载帮助\n"
            "/bili_login  扫码登录 B 站（建议使用小号）\n"
            "/bili_status 查看登录状态\n"
            "/bili_search 关键词  搜索视频\n"
            "/bili_down 编号      下载搜索结果（如 /bili_down 1）\n"
            "/bili_down BV号       下载指定视频\n"
            "/bili_down 链接       下载指定视频/分P\n"
            "也可以在插件 WebUI 中搜索并直接下载音频文件。"
        )
        yield event.plain_result(text)

    @filter.command("bili_login", alias={"b站登录", "bili扫码"})
    async def bili_login(self, event: AstrMessageEvent):
        """获取 Bilibili 扫码登录二维码。"""
        try:
            await self._ensure_qrcode(force=True)
        except Exception as exc:
            logger.exception("Failed to get Bilibili login QR code")
            yield event.plain_result(f"获取二维码失败：{exc}")
            return
        if not self.qrcode_url:
            yield event.plain_result("获取二维码失败，请稍后再试。")
            return
        png = await asyncio.to_thread(self._build_qr_png)
        if png:
            yield event.chain_result(
                [
                    Image.fromBytes(png),
                    Plain("\n请用哔哩哔哩 APP 扫码登录，确认后发送 /bili_status 查看状态。"),
                ]
            )
        else:
            qr_img = (
                "https://api.qrserver.com/v1/create-qr-code/?size=360x360&data="
                + urllib.parse.quote(self.qrcode_url, safe="")
            )
            yield event.image_result(qr_img)
            yield event.plain_result(
                "请用哔哩哔哩 APP 扫码登录，确认后发送 /bili_status 查看状态。"
            )

    @filter.command("bili_refresh", alias={"刷新二维码", "bili刷新"})
    async def bili_refresh(self, event: AstrMessageEvent):
        """刷新二维码。"""
        self.qrcode_key = ""
        self.qrcode_url = ""
        try:
            await self._ensure_qrcode(force=True)
        except Exception as exc:
            logger.exception("Failed to refresh Bilibili login QR code")
            yield event.plain_result(f"刷新二维码失败：{exc}")
            return
        if not self.qrcode_url:
            yield event.plain_result("刷新二维码失败，请稍后再试。")
            return
        png = await asyncio.to_thread(self._build_qr_png)
        if png:
            yield event.chain_result(
                [
                    Image.fromBytes(png),
                    Plain("\n二维码已刷新，请用哔哩哔哩 APP 扫码，确认后发送 /bili_status 查看状态。"),
                ]
            )
        else:
            qr_img = (
                "https://api.qrserver.com/v1/create-qr-code/?size=360x360&data="
                + urllib.parse.quote(self.qrcode_url, safe="")
            )
            yield event.image_result(qr_img)
            yield event.plain_result(
                "二维码已刷新，请用哔哩哔哩 APP 扫码，确认后发送 /bili_status 查看状态。"
            )

    @filter.command("bili_status", alias={"b站状态"})
    async def bili_status(self, event: AstrMessageEvent):
        """查看 Bilibili 登录状态。"""
        if self.qrcode_key:
            try:
                await self._poll_login()
            except Exception as exc:
                logger.warning("Polling Bilibili login failed: %s", exc)
        status = await self._check_login_status()
        yield event.plain_result(status)

    @filter.command("bili_search", alias={"b站搜索"})
    async def bili_search(self, event: AstrMessageEvent):
        """搜索 Bilibili 视频：/bili_search 关键词"""
        keyword = self._command_body(
            event.get_message_str(),
            ["/bili_search", "bili_search", "/b站搜索", "b站搜索"],
        ).strip()
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
            yield event.plain_result("没有搜到结果，换个关键词试试。")
            return

        session = self._session_key(event)
        self.last_search[session] = results
        if len(self.last_search) > 50:
            for key in list(self.last_search)[:-50]:
                self.last_search.pop(key, None)

        lines = [f"搜索结果（共 {len(results)} 条）："]
        for idx, item in enumerate(results, 1):
            title = item.get("title", "无标题")
            author = item.get("author", "未知UP")
            duration = item.get("duration", "")
            bvid = item.get("bvid", "")
            page = item.get("page")
            page_tag = f" [P{page}]" if page else ""
            lines.append(f"编号 {idx}. {title}")
            lines.append(f"   UP {author} / {duration} / {bvid}{page_tag}")
        lines.append("下载：/bili_down 编号（如 /bili_down 1）")
        lines.append("或直接：/bili_down BV号 / b23.tv 链接")
        yield event.plain_result("\n".join(lines))

    @filter.command("bili_down", alias={"b站下载"})
    async def bili_down(self, event: AstrMessageEvent):
        """下载搜索结果或链接并转成 QQ 语音：/bili_down 1 或 /bili_down BV..."""
        arg = self._command_body(
            event.get_message_str(),
            ["/bili_down", "bili_down", "/b站下载", "b站下载"],
        ).strip()
        if not arg:
            yield event.plain_result(
                "用法：\n"
                "/bili_down 编号（先 /bili_search 搜索）\n"
                "/bili_down BV号\n"
                "/bili_down https://www.bilibili.com/video/BVxxx?p=1"
            )
            return

        try:
            url, page = self._resolve_download_url(self._session_key(event), arg)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        p_msg = f" (P{page})" if page > 0 else ""
        yield event.plain_result(f"开始下载并转换音频{p_msg}，请稍等...")
        try:
            audio_path, is_voice = await self._run_download(url, page)
        except Exception as exc:
            logger.exception("Bilibili download failed")
            yield event.plain_result("下载失败：" + self._format_download_error(exc))
            return

        ok = await self._send_audio(event, audio_path, is_voice)
        if not ok:
            yield event.plain_result(
                "音频发送失败，文件已保存在服务器：\n"
                f"{audio_path}\n"
                "可尝试在插件 WebUI 中直接下载该文件。"
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        try:
            return str(event.session)
        except Exception:
            return str(event.get_sender_id() or "")

    async def _ensure_qrcode(self, force: bool = False):
        if self.qrcode_key and self.qrcode_url and not force:
            return
        async with httpx.AsyncClient(headers=HTTP_HEADERS, timeout=15) as client:
            resp = await client.get(BILI_QR_GENERATE)
            resp.raise_for_status()
            data = resp.json().get("data") or {}
            self.qrcode_key = str(data.get("qrcode_key") or "")
            self.qrcode_url = str(data.get("url") or "")
            self.login_status = "请使用 Bilibili 手机端扫码登录"

    def _build_qr_png(self) -> bytes | None:
        if not self.qrcode_url:
            return None
        try:
            import qrcode

            image = qrcode.make(self.qrcode_url)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
        except Exception as exc:
            logger.warning(f"Local QR generation failed: {exc}")
            return None

    async def _poll_login(self):
        async with httpx.AsyncClient(headers=HTTP_HEADERS, timeout=15) as client:
            resp = await client.get(BILI_QR_POLL, params={"qrcode_key": self.qrcode_key})
            resp.raise_for_status()
            payload = resp.json().get("data") or {}
            code = int(payload.get("code", -1))
            if code == 0:
                self._save_cookies(client.cookies)
                self.qrcode_key = ""
                self.qrcode_url = ""
                self.login_status = "已登录"
                await self._save_state()
            elif code == 86038:
                self.login_status = "二维码已过期，请发送 /bili_refresh 刷新"
            elif code == 86090:
                self.login_status = "已扫码，请在手机上确认登录"
            else:
                self.login_status = str(payload.get("message") or "") or "等待扫码"

    async def _check_login_status(self) -> str:
        cookies = self._load_cookie_dict()
        if not cookies:
            return "未登录。发送 /bili_login 获取二维码扫码登录。"
        try:
            async with httpx.AsyncClient(
                headers=HTTP_HEADERS, cookies=cookies, timeout=15
            ) as client:
                resp = await client.get(BILI_NAV)
                if resp.status_code != 200:
                    return "登录状态检查失败，请稍后再试。"
                data = resp.json()
                if data.get("code") == 0 and data.get("data", {}).get("isLogin"):
                    name = data.get("data", {}).get("uname") or "Bilibili 用户"
                    return f"已登录：{name}"
                return "登录已失效。发送 /bili_login 重新扫码。"
        except Exception as exc:
            logger.warning("Bilibili login status check failed: %s", exc)
            return f"登录状态检查失败：{exc}"

    async def _search(self, keyword: str) -> list[dict[str, Any]]:
        keyword = keyword.strip()
        cookies = self._load_cookie_dict() or dict(BILI_BASE_COOKIES)

        # Direct BV number lookup
        if BV_PATTERN.match(keyword):
            bvid = "BV" + keyword[2:]
            async with httpx.AsyncClient(
                headers=HTTP_HEADERS, cookies=cookies, timeout=20
            ) as client:
                resp = await client.get(BILI_VIDEO_INFO, params={"bvid": bvid})
                resp.raise_for_status()
                payload = resp.json()
            if payload.get("code") != 0 or not payload.get("data"):
                message = payload.get("message") or "未找到该视频"
                raise RuntimeError(message)
            video = payload["data"]
            title = str(video.get("title") or keyword)
            author = str(video.get("owner", {}).get("name") or "未知UP")
            pages = video.get("pages") or []
            try:
                video_count = int(video.get("videos", 1) or 1)
            except (TypeError, ValueError):
                video_count = 1
            if video_count > 1 and pages:
                results = []
                for index, page in enumerate(pages, 1):
                    results.append(
                        {
                            "title": f"{title} - {page.get('part') or f'P{index}'}",
                            "author": author,
                            "duration": self._format_duration(page.get("duration")),
                            "bvid": bvid,
                            "url": f"{BILI_REFERER}video/{bvid}?p={index}",
                            "page": index,
                        }
                    )
                return results

            return [
                {
                    "title": title,
                    "author": author,
                    "duration": self._format_duration(video.get("duration")),
                    "bvid": bvid,
                    "url": f"{BILI_REFERER}video/{bvid}",
                    "page": 1,
                }
            ]

        params = {"search_type": "video", "keyword": keyword, "page": 1}
        async with httpx.AsyncClient(
            headers={**HTTP_HEADERS, "Origin": "https://search.bilibili.com"},
            cookies=cookies,
            timeout=20,
        ) as client:
            resp = await client.get(BILI_SEARCH, params=params)
            resp.raise_for_status()
            payload = resp.json()
        if payload.get("code") != 0:
            message = payload.get("message") or "B站搜索接口返回错误"
            raise RuntimeError(message)

        data = payload.get("data") or {}
        items: list[dict[str, Any]] = []
        for raw in (data.get("result") or [])[: self.search_limit]:
            title = self._strip_tags(str(raw.get("title") or "无标题"))
            bvid = str(raw.get("bvid") or "")
            arcurl = str(raw.get("arcurl") or "")
            if not bvid and not arcurl:
                continue
            items.append(
                {
                    "title": title,
                    "author": str(raw.get("author") or "未知UP"),
                    "duration": str(raw.get("duration") or ""),
                    "bvid": bvid,
                    "url": arcurl or f"{BILI_REFERER}video/{bvid}",
                    "cover": str(raw.get("pic") or ""),
                }
            )
        return items

    async def _run_download(self, url: str, page: int) -> tuple[str, bool]:
        key = f"{url}|{page}"
        lock = self._download_locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await asyncio.to_thread(self._download_audio, url, page)

    def _download_audio(self, url: str, page: int = 0) -> tuple[str, bool]:
        """Download and (when possible) transcode audio.

        Returns (path, is_voice_ready). ``is_voice_ready`` is True only when the
        file is a WAV that can be sent as a QQ voice message.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        url, page = self._normalize_page(url, page)
        has_ffmpeg = self._has_ffmpeg()

        opts: dict[str, Any] = {
            "format": "bestaudio/best",
            "outtmpl": str(self.data_dir / "%(title).100s-%(id)s.%(ext)s"),
            "noplaylist": page <= 0,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "http_headers": DOWNLOAD_HEADERS,
        }
        if page > 0:
            opts["playlist_items"] = str(page)
        cookie_file = self._cookie_file_for_download()
        if cookie_file:
            opts["cookiefile"] = str(cookie_file)
        if self.ffmpeg_path:
            opts["ffmpeg_location"] = self.ffmpeg_path
        if self.proxy:
            opts["proxy"] = self.proxy
        if self.max_duration > 0:
            limit = self.max_duration

            def match_filter(info, *, incomplete=False):
                try:
                    duration = int(float(info.get("duration") or 0))
                except (TypeError, ValueError):
                    duration = 0
                if duration and duration > limit:
                    raise yt_dlp.utils.DownloadError(
                        f"视频时长 {duration} 秒，超过插件限制 {limit} 秒；"
                        "可在插件配置中把 max_duration_seconds 设为 0 取消限制"
                    )
                return None

            opts["match_filter"] = match_filter

        if has_ffmpeg:
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "wav",
                    "preferredquality": "0",
                }
            ]
            opts["postprocessor_args"] = {"ffmpeg": ["-ar", "24000", "-ac", "1"]}
        else:
            # No ffmpeg: still download the original audio stream so the user
            # can get the file instead of a hard failure.
            opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
            logger.warning(
                "ffmpeg not found; downloading original audio without WAV conversion"
            )

        started = time.time()
        raw_fallback = False
        try:
            candidates = self._download_once(opts, url)
        except Exception as exc:
            message = str(exc).lower()
            ffmpeg_failure = any(
                marker in message
                for marker in ("ffmpeg", "ffprobe", "postprocess", "postprocessing")
            )
            if not (has_ffmpeg and ffmpeg_failure):
                raise
            # ffmpeg exists but cannot convert this stream (or ffprobe is
            # missing). Retry without post-processing and send the original
            # audio as a file instead of failing the whole download.
            logger.warning("Audio post-processing failed, retrying raw: %s", exc)
            opts.pop("postprocessors", None)
            opts.pop("postprocessor_args", None)
            opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
            started = time.time()
            raw_fallback = True
            candidates = self._download_once(opts, url)

        resolved = self._pick_output(
            candidates, started, exclude={".wav"} if raw_fallback else set()
        )
        if resolved is None:
            raise RuntimeError(
                "音频文件未生成。请确认 ffmpeg 已安装并加入 PATH，"
                "或在插件配置中设置 ffmpeg_path。"
            )
        return str(resolved.resolve()), resolved.suffix.lower() == ".wav"

    def _download_once(self, opts: dict[str, Any], url: str) -> list[str]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return self._collect_output_candidates(info, ydl)

    def _download_mp3(self, url: str, page: int = 0) -> str:
        """Backward-compatible wrapper used by older code/tests."""
        return self._download_audio(url, page)[0]

    def _collect_output_candidates(
        self, info: dict[str, Any], ydl: yt_dlp.YoutubeDL
    ) -> list[str]:
        candidates: list[str] = []

        def walk(node: Any):
            if not isinstance(node, dict):
                return
            for download in node.get("requested_downloads") or []:
                if isinstance(download, dict):
                    for key in ("filepath", "_filename", "filename"):
                        value = download.get(key)
                        if value:
                            candidates.append(str(value))
            for key in ("filepath", "_filename", "filename"):
                value = node.get(key)
                if value:
                    candidates.append(str(value))
            try:
                candidates.append(ydl.prepare_filename(node))
            except Exception:
                pass
            for entry in node.get("entries") or []:
                walk(entry)

        walk(info)
        return candidates

    def _pick_output(
        self, candidates: list[str], started: float, exclude: set[str] | None = None
    ) -> Path | None:
        excluded = {suffix.lower() for suffix in (exclude or set())}
        checked: set[str] = set()
        existing: list[Path] = []
        for raw in candidates:
            base = Path(raw)
            variants = [
                base.with_suffix(".wav"),
                base.with_suffix(".m4a"),
                base.with_suffix(".mp3"),
                base.with_suffix(".webm"),
                base.with_suffix(".opus"),
                base,
            ]
            for candidate in variants:
                key = str(candidate).lower()
                if key in checked:
                    continue
                checked.add(key)
                if candidate.suffix.lower() in excluded:
                    continue
                try:
                    if candidate.is_file():
                        existing.append(candidate)
                        if candidate.stat().st_mtime >= started - 10:
                            return candidate
                except OSError:
                    continue

        # yt-dlp may skip an already-completed download. Prefer the exact
        # filename for this video over the "newest file" fallback.
        for candidate in existing:
            return candidate

        # Last resort: a recently written audio file in the download directory.
        matches: list[Path] = []
        for pattern in ("*.wav", "*.m4a", "*.mp3", "*.webm", "*.opus"):
            if Path(pattern).suffix.lower() in excluded:
                continue
            for candidate in self.data_dir.glob(pattern):
                try:
                    if candidate.is_file() and candidate.stat().st_mtime >= started - 10:
                        matches.append(candidate)
                except OSError:
                    continue
        if matches:
            logger.warning(
                "Falling back to newest audio file in %s for this download",
                self.data_dir,
            )
            return max(matches, key=lambda item: item.stat().st_mtime)
        return None

    def _normalize_page(self, url: str, page: int) -> tuple[str, int]:
        if page <= 0:
            match = PAGE_IN_TEXT.search(url)
            if match:
                page = int(match.group(1))
        return url, page

    def _resolve_download_url(self, session: str, arg: str) -> tuple[str, int]:
        """Return (url, page_number). page=0 means auto/first page."""
        if arg.isdigit():
            items = self.last_search.get(session) or []
            index = int(arg) - 1
            if index < 0 or index >= len(items):
                raise ValueError("编号不存在或搜索结果已过期，请重新 /bili_search 搜索")
            item = items[index]
            try:
                page = int(item.get("page", 0) or 0)
            except (TypeError, ValueError):
                page = 0
            return str(item["url"]), page

        if arg.startswith(("http://", "https://")):
            _, page = self._normalize_page(arg, 0)
            return arg, page

        match = BV_IN_TEXT.search(arg)
        if match:
            bvid = "BV" + match.group(0)[2:]
            page_match = PAGE_IN_TEXT.search(arg)
            page = int(page_match.group(1)) if page_match else 0
            return f"{BILI_REFERER}video/{bvid}", page

        if arg.lower().startswith("av") and arg[2:].isdigit():
            return f"{BILI_REFERER}video/{arg}", 0

        raise ValueError(
            "无法识别的下载参数，请使用编号、BV 号或 B 站视频链接。"
        )

    async def _send_audio(
        self, event: AstrMessageEvent, file_path: str, is_voice: bool
    ) -> bool:
        """Send audio through AstrBot's message components.

        AstrBot handles platform-specific conversion (WAV -> SILK for OneBot)
        and works across Docker/NapCat boundaries, unlike raw file:/// paths.
        """
        path = Path(file_path)
        if not path.is_file():
            logger.error("Audio file does not exist: %s", file_path)
            return False

        try:
            file_size = path.stat().st_size
        except OSError:
            file_size = 0

        if is_voice and self.send_as_voice and file_size <= VOICE_MAX_BYTES:
            try:
                await event.send(
                    MessageChain([Record.fromFileSystem(str(path))])
                )
                return True
            except Exception as exc:
                logger.warning("Sending record failed, falling back to file: %s", exc)
        elif is_voice and file_size > VOICE_MAX_BYTES:
            logger.info(
                "Audio is %.1f MiB; sending as file instead of voice",
                file_size / 1024 / 1024,
            )

        try:
            await event.send(
                MessageChain([File(name=path.name, file=str(path))])
            )
            return True
        except Exception as exc:
            logger.error("Sending audio file failed: %s", exc)
            return False

    def _format_download_error(self, exc: Exception) -> str:
        text = ANSI_PATTERN.sub("", str(exc) or exc.__class__.__name__).strip()
        lower = text.lower()
        if "ffmpeg" in lower:
            return (
                "未找到 ffmpeg，无法把音频转换为语音。\n"
                "请安装 ffmpeg 并加入 PATH，或在插件配置中填写 ffmpeg_path。\n"
                f"详细信息：{text}"
            )
        if "567" in text:
            return (
                "B 站 CDN 返回 567（腾讯 EdgeOne 拦截）。插件已自动带上 Range 头，"
                "若仍失败，通常是当前服务器 IP 被 B 站 CDN 限制。\n"
                "请尝试：1) 在配置中填写可用的 HTTP 代理 proxy；"
                "2) 换一台网络环境不同的服务器；3) 登录 B 站账号后重试。\n"
                f"详细信息：{text}"
            )
        if "412" in text or "403" in text or "login" in lower or "登录" in text:
            return (
                "B 站拒绝了本次请求，通常是未登录或触发风控。\n"
                "请先发送 /bili_login 扫码登录（建议使用小号）后再试。\n"
                f"详细信息：{text}"
            )
        return text

    def _has_ffmpeg(self) -> bool:
        if self.ffmpeg_path:
            candidate = Path(self.ffmpeg_path).expanduser()
            if candidate.is_dir():
                candidate = candidate / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
            return candidate.is_file()
        return shutil.which("ffmpeg") is not None

    def _ensure_anon_cookie_file(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.anon_cookie_file.exists():
            expires = str(int(time.time() + 31536000))
            lines = ["# Netscape HTTP Cookie File"]
            for name, value in BILI_BASE_COOKIES.items():
                lines.append(
                    "\t".join(
                        [".bilibili.com", "TRUE", "/", "FALSE", expires, name, value]
                    )
                )
            self.anon_cookie_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.anon_cookie_file

    def _cookie_file_for_download(self) -> Path | None:
        if self.cookie_file.exists() and self.cookie_file.stat().st_size > 0:
            return self.cookie_file
        return self._ensure_anon_cookie_file()

    def _save_cookies(self, cookies: httpx.Cookies):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        lines = ["# Netscape HTTP Cookie File"]
        existing: set[str] = set()
        for cookie in cookies.jar:
            existing.add(cookie.name)
            domain = cookie.domain or ".bilibili.com"
            domain_flag = "TRUE" if domain.startswith(".") else "FALSE"
            secure = "TRUE" if cookie.secure else "FALSE"
            expires = str(int(cookie.expires or (time.time() + 31536000)))
            prefix = ""
            try:
                if cookie.has_nonstandard_attr("HttpOnly"):
                    prefix = "#HttpOnly_"
            except Exception:
                pass
            lines.append(
                "\t".join(
                    [
                        prefix + domain,
                        domain_flag,
                        cookie.path or "/",
                        secure,
                        expires,
                        cookie.name,
                        cookie.value,
                    ]
                )
            )

        expires = str(int(time.time() + 31536000))
        for name, value in BILI_BASE_COOKIES.items():
            if name not in existing:
                lines.append(
                    "\t".join(
                        [".bilibili.com", "TRUE", "/", "FALSE", expires, name, value]
                    )
                )
        self.cookie_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _load_cookie_dict(self) -> dict[str, str]:
        if not self.cookie_file.exists():
            return {}
        cookies: dict[str, str] = {}
        for line in self.cookie_file.read_text(
            encoding="utf-8", errors="ignore"
        ).splitlines():
            line = line.removeprefix("#HttpOnly_")
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
        self.state_file.write_text(
            json.dumps({"login_status": self.login_status}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Pages / Views Web API handlers
    # ------------------------------------------------------------------

    async def api_qrcode(self):
        try:
            await self._ensure_qrcode(force=True)
        except Exception as exc:
            logger.exception("Failed to get Bilibili login QR code")
            return error_response(f"获取二维码失败：{exc}")
        return json_response(self._qrcode_payload())

    async def api_qrcode_refresh(self):
        self.qrcode_key = ""
        self.qrcode_url = ""
        try:
            await self._ensure_qrcode(force=True)
        except Exception as exc:
            logger.exception("Failed to refresh Bilibili login QR code")
            return error_response(f"刷新二维码失败：{exc}")
        return json_response(self._qrcode_payload())

    def _qrcode_payload(self) -> dict[str, Any]:
        png = self._build_qr_png()
        image = ""
        if png:
            image = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        return {
            "qrcode_url": self.qrcode_url,
            "qrcode_key": self.qrcode_key,
            "image": image,
        }

    async def api_status(self):
        if self.qrcode_key:
            try:
                await self._poll_login()
            except Exception as exc:
                logger.warning("Polling Bilibili login failed: %s", exc)
        cookies = self._load_cookie_dict()
        if not cookies:
            return json_response(
                {
                    "logged_in": False,
                    "nickname": "",
                    "username": request.username,
                }
            )
        async with httpx.AsyncClient(
            headers=HTTP_HEADERS, cookies=cookies, timeout=15
        ) as client:
            resp = await client.get(BILI_NAV)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0 and data.get("data", {}).get("isLogin"):
                    name = data.get("data", {}).get("uname") or ""
                    return json_response(
                        {
                            "logged_in": True,
                            "nickname": name,
                            "username": request.username,
                        }
                    )
        return json_response(
            {"logged_in": False, "nickname": "", "username": request.username}
        )

    async def api_search(self):
        keyword = request.query.get("keyword", "")
        if not keyword:
            return error_response("keyword is required")
        try:
            results = await self._search(keyword)
        except Exception as exc:
            logger.exception("Bilibili search failed")
            return error_response(str(exc))
        return json_response({"results": results})

    async def api_download(self):
        url = request.query.get("url", "")
        page = request.query.get("page", 0, type=int) or 0
        if not url:
            return error_response("url is required")
        try:
            audio_path, is_voice = await self._run_download(url, page)
        except Exception as exc:
            logger.exception("Bilibili download failed")
            return error_response(self._format_download_error(exc))

        token = secrets.token_urlsafe(24)
        self._download_tokens[token] = {
            "path": audio_path,
            "expires": time.time() + COOKIE_TTL,
        }
        self._prune_download_tokens()
        try:
            size = Path(audio_path).stat().st_size
        except OSError:
            size = 0
        return json_response(
            {
                "success": True,
                "file": audio_path,
                "filename": Path(audio_path).name,
                "is_voice": is_voice,
                "size": size,
                "token": token,
            }
        )

    async def api_download_file(self):
        if file_response is None:
            return error_response(
                "当前 AstrBot 版本不支持文件下载，请升级到 4.26.0 或更高版本。",
                status_code=500,
            )
        token = request.query.get("token", "")
        entry = self._download_tokens.get(token)
        if not entry:
            return error_response("下载链接无效或已过期，请重新下载。", status_code=404)
        if float(entry["expires"]) < time.time():
            self._download_tokens.pop(token, None)
            return error_response("下载链接已过期，请重新下载。", status_code=410)

        path = Path(str(entry["path"]))
        if not path.is_file():
            self._download_tokens.pop(token, None)
            return error_response("文件已被清理，请重新下载。", status_code=404)

        content_type = AUDIO_CONTENT_TYPES.get(path.suffix.lower()) or (
            mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        return file_response(path, filename=path.name, content_type=content_type)

    def _prune_download_tokens(self):
        now = time.time()
        for key, value in list(self._download_tokens.items()):
            if float(value.get("expires", 0)) < now:
                self._download_tokens.pop(key, None)
        while len(self._download_tokens) > MAX_DOWNLOAD_TOKENS:
            oldest = min(
                self._download_tokens,
                key=lambda item: float(self._download_tokens[item].get("expires", 0)),
            )
            self._download_tokens.pop(oldest, None)

    # ------------------------------------------------------------------
    # Small utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_tags(text: str) -> str:
        return html.unescape(TAG_PATTERN.sub("", text)).strip()

    @staticmethod
    def _format_duration(seconds: Any) -> str:
        try:
            total = int(float(seconds or 0))
        except (TypeError, ValueError):
            return ""
        minutes, sec = divmod(max(total, 0), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{sec:02d}" if hours else f"{minutes}:{sec:02d}"

    @staticmethod
    def _command_body(message: str, names: list[str]) -> str:
        text = str(message or "").strip()
        for name in names:
            if text.startswith(name):
                return text[len(name) :].strip()
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""
