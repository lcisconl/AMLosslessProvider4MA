"""ALAC streaming for the experimental Apple Music Lossless provider."""

from __future__ import annotations

import base64
import re
import time
from contextlib import aclosing
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urljoin

from aiohttp import ClientError, ClientTimeout, web
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.providers.apple_music.streaming import AppleMusicStreamingManager
from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails
from pywidevine import PSSH, Cdm, Device, DeviceTypes
from shortuuid import uuid

from .cache import TrackCache, TrackCacheWriter

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

if TYPE_CHECKING:
    from music_assistant.providers.apple_music_lossless.provider import (
        AppleMusicLosslessProvider,
    )

_HLS_ATTRIBUTE_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
_LICENSE_URL = "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/acquireWebPlaybackLicense"
_ASSET_TTL = 6 * 60 * 60


class AppleMusicLosslessStreamingManager(AppleMusicStreamingManager):
    """Resolve catalog ALAC HLS and decrypt it through Widevine + a local key route."""

    def __init__(self, provider: AppleMusicLosslessProvider) -> None:
        """Initialize the lossless streaming manager."""
        super().__init__(provider)
        self.provider = provider
        self._route_prefix = f"/apple_music_lossless/{provider.instance_id}"
        self._route_remove: Any = None
        self._playlists: dict[str, tuple[float, str]] = {}
        self._keys: dict[str, tuple[float, bytes]] = {}
        self._key_cache: dict[tuple[str, str, str], bytes] = {}
        self._license_urls_cache: dict[str, tuple[str, ...]] = {}
        self._track_cache: TrackCache | None = None
        self._web_token: str | None = None
        self._web_token_expires = 0.0
        self._fairplay_gateway = ""
        self._fairplay_token = ""
        self.logger.debug("Apple Music lossless manager initialised")

    @property
    def route_url(self) -> str:
        """Return the local route prefix, for diagnostics."""
        return self._route_prefix

    @property
    def cdm_available(self) -> bool:
        """Return whether the Widevine CDM is loaded."""
        return bool(self._decrypt_client_id and self._decrypt_private_key)

    def register_routes(self) -> None:
        """Register the local HLS playlist/key route once."""
        if self._route_remove is not None:
            return
        path = f"{self._route_prefix}/*"
        try:
            self._route_remove = self.provider.mass.webserver.register_dynamic_route(
                path, self._handle_route
            )
        except RuntimeError:
            # A provider reload leaves the previous instance's route behind; replace
            # it so the route points at the current manager's asset dictionaries.
            self.provider.mass.webserver.unregister_dynamic_route(path)
            self._route_remove = self.provider.mass.webserver.register_dynamic_route(
                path, self._handle_route
            )

    def configure_fairplay(self, gateway: str, token: str) -> None:
        """Configure the external FairPlay gateway."""
        self._fairplay_gateway = gateway.strip().rstrip("/")
        self._fairplay_token = token.strip()

    async def configure_cache(
        self, enabled: bool, max_size_mb: int, directory: Path
    ) -> None:
        """Configure and prepare the on-disk track cache."""
        self._track_cache = TrackCache(
            self.logger,
            directory,
            max_size_mb * 1024 * 1024,
            enabled,
        )
        await self._track_cache.initialize()

    async def cache_diagnostics(self) -> dict[str, Any]:
        """Return track cache statistics for the provider diagnostics."""
        if self._track_cache is None:
            return {"enabled": False}
        return await self._track_cache.stats()

    async def close(self) -> None:
        """Stop background cache maintenance."""
        if self._track_cache is not None:
            await self._track_cache.close()

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return ALAC stream details, falling back to the regular AAC path."""
        self.logger.debug("Apple Music lossless check for %s", item_id)
        if item_id.isnumeric():
            cached = self._get_cached_stream_details(item_id)
            if cached is not None:
                return cached
            try:
                if self.fairplay_enabled:
                    fairplay = await self._get_fairplay_stream_details(item_id)
                    if fairplay is not None:
                        return fairplay
                lossless = await self._get_lossless_stream_details(item_id)
                if lossless is not None:
                    return lossless
            except (
                ClientError,
                MediaNotFoundError,
                TimeoutError,
                KeyError,
                TypeError,
                ValueError,
            ) as err:
                self.logger.warning(
                    "Apple Music lossless unavailable for %s, falling back to AAC: %s",
                    item_id,
                    err,
                )
        return await super().get_stream_details(item_id)

    @property
    def fairplay_enabled(self) -> bool:
        """Return whether the Adam FairPlay gateway is configured."""
        return bool(self._fairplay_gateway)

    @property
    def _fairplay_headers(self) -> dict[str, str]:
        """Return headers for the FairPlay sidecar."""
        if not self._fairplay_token:
            return {}
        return {"x-api-token": self._fairplay_token}

    async def _get_fairplay_stream_details(self, item_id: str) -> StreamDetails | None:
        """Create an Adam FairPlay playback session for a lossless track."""
        quality = await self._get_alac_variant(item_id)
        if quality is None:
            return None
        async with self.provider.mass.http_session.get(
            f"{self._fairplay_gateway}/api/v1/capabilities",
            headers=self._fairplay_headers,
            ssl=False,
            timeout=ClientTimeout(total=10),
        ) as response:
            response.raise_for_status()
            capabilities = cast("dict[str, Any]", await response.json(loads=json_loads))
        if capabilities.get("lossless") is not True:
            return None
        web_token = await self._get_web_token()
        if not web_token:
            return None
        payload = {
            "assetId": item_id,
            "storefront": self.provider._storefront or "us",
            "token": web_token,
            "mediaUserToken": self.provider._music_user_token or "",
            "capabilities": {"lossless": True, "hiRes": True},
        }
        async with self.provider.mass.http_session.post(
            f"{self._fairplay_gateway}/api/v1/playback",
            json=payload,
            headers=self._fairplay_headers,
            ssl=False,
            timeout=ClientTimeout(total=60),
        ) as response:
            response.raise_for_status()
            session = cast("dict[str, Any]", await response.json(loads=json_loads))
        if str(session.get("codec", "")).lower() != "alac":
            return None
        session_id = session.get("sessionId")
        if not session_id:
            return None
        (sample_rate, bit_depth), _ = quality
        self.logger.debug(
            "Apple Music FairPlay: %s -> ALAC %s/%s session=%s",
            item_id,
            sample_rate,
            bit_depth,
            session_id,
        )
        return StreamDetails(
            item_id=item_id,
            provider=self.provider.instance_id,
            stream_type=StreamType.CUSTOM,
            audio_format=AudioFormat(
                content_type=ContentType.MP4,
                codec_type=ContentType.ALAC,
                sample_rate=sample_rate,
                bit_depth=bit_depth,
            ),
            can_seek=True,
            allow_seek=True,
            expiration=3600,
            data={"fairplay_session": session_id},
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """Yield decrypted ALAC bytes, filling or serving the on-disk track cache."""
        cache = self._track_cache
        if cache is not None and cache.enabled and seek_position <= 0:
            cached = cache.find(streamdetails.item_id)
            if cached is not None:
                self.logger.debug(
                    "Apple Music lossless: cached track for %s", streamdetails.item_id
                )
                async with aclosing(cache.iterate(cached)) as stream:
                    async for chunk in stream:
                        yield chunk
                return
        data = streamdetails.data if isinstance(streamdetails.data, dict) else {}
        session_id = data.get("fairplay_session")
        if not session_id or not self._fairplay_gateway:
            raise MediaNotFoundError("No FairPlay playback session for this stream")
        if cache is not None and cache.enabled and seek_position <= 0:
            sample_rate = int(getattr(streamdetails.audio_format, "sample_rate", 0) or 0)
            bit_depth = int(getattr(streamdetails.audio_format, "bit_depth", 0) or 0)
            writer = None
            if sample_rate > 0 and bit_depth > 0:
                writer = await cache.begin_write(
                    streamdetails.item_id, sample_rate, bit_depth
                )
            if writer is not None:
                async with aclosing(
                    self._stream_and_cache(streamdetails, str(session_id), writer)
                ) as stream:
                    async for chunk in stream:
                        yield chunk
                return
        async with aclosing(
            self._gateway_stream(streamdetails, str(session_id), seek_position)
        ) as stream:
            async for chunk in stream:
                yield chunk

    async def _stream_and_cache(
        self,
        streamdetails: StreamDetails,
        session_id: str,
        writer: TrackCacheWriter,
    ) -> AsyncGenerator[bytes]:
        """Yield the gateway stream while writing a copy to the track cache."""
        cache = self._track_cache
        completed = False
        expected_size: list[int] = []
        try:
            async with aclosing(
                self._gateway_stream(streamdetails, session_id, 0, expected_size)
            ) as stream:
                async for chunk in stream:
                    await writer.write(chunk)
                    yield chunk
            if not writer.closed and expected_size and writer.written != expected_size[0]:
                self.logger.warning(
                    "Apple Music lossless: incomplete stream for %s (%s/%s bytes), "
                    "not caching it",
                    streamdetails.item_id,
                    writer.written,
                    expected_size[0],
                )
                writer.abort()
            else:
                writer.commit()
            completed = True
        finally:
            if not completed:
                writer.abort()
            if cache is not None:
                cache.end_write(streamdetails.item_id)

    async def _gateway_stream(
        self,
        streamdetails: StreamDetails,
        session_id: str,
        seek_position: int,
        expected_size: list[int] | None = None,
    ) -> AsyncGenerator[bytes]:
        """Yield decrypted ALAC bytes from the Adam FairPlay sidecar."""
        url = f"{self._fairplay_gateway}/api/v1/playback/{session_id}/audio"
        if seek_position > 0:
            url = f"{url}?t={int(seek_position)}"
        try:
            async with self.provider.mass.http_session.get(
                url,
                headers=self._fairplay_headers,
                ssl=False,
                timeout=ClientTimeout(total=None, connect=10, sock_read=60),
            ) as response:
                response.raise_for_status()
                if expected_size is not None and response.content_length is not None:
                    expected_size.append(response.content_length)
                async for chunk in response.content.iter_chunked(128 * 1024):
                    if chunk:
                        yield chunk
        except (ClientError, TimeoutError) as err:
            raise MediaNotFoundError(f"FairPlay gateway stream failed: {err}") from err

    def _get_cached_stream_details(self, item_id: str) -> StreamDetails | None:
        """Return stream details pointing at a cached track, if one exists."""
        cache = self._track_cache
        if cache is None or not cache.enabled:
            return None
        track = cache.lookup(item_id)
        if track is None:
            return None
        self.logger.debug(
            "Apple Music lossless: cache hit for %s (%s/%s)",
            item_id,
            track.sample_rate,
            track.bit_depth,
        )
        return StreamDetails(
            item_id=item_id,
            provider=self.provider.instance_id,
            path=str(track.path),
            stream_type=StreamType.LOCAL_FILE,
            audio_format=AudioFormat(
                content_type=ContentType.MP4,
                codec_type=ContentType.ALAC,
                sample_rate=track.sample_rate,
                bit_depth=track.bit_depth,
            ),
            can_seek=True,
            allow_seek=True,
            size=track.size,
            expiration=3600,
            data={"cached": True},
        )

    async def _get_lossless_stream_details(self, item_id: str) -> StreamDetails | None:
        """Resolve one ALAC media playlist and publish its keys on the local route."""
        variant = await self._get_alac_variant(item_id)
        if variant is None:
            return None
        (sample_rate, bit_depth), media_url = variant
        media_playlist = await self._fetch_text(media_url)
        rewritten = await self._rewrite_playlist(item_id, media_url, media_playlist)
        if rewritten is None:
            self.logger.debug(
                "Apple Music lossless: no Widevine key material in ALAC playlist for %s",
                item_id,
            )
            return None
        playlist, key_tokens = rewritten
        token = str(uuid())
        expires = time.time() + _ASSET_TTL
        self._playlists[token] = (expires, playlist)
        self._prune_assets()
        self.logger.debug(
            "Apple Music lossless: %s -> ALAC %s/%s (%s keys)",
            item_id,
            sample_rate,
            bit_depth,
            len(key_tokens),
        )
        return StreamDetails(
            item_id=item_id,
            provider=self.provider.instance_id,
            path=self._asset_url("playlist", token),
            stream_type=StreamType.HLS,
            audio_format=AudioFormat(
                content_type=ContentType.MP4,
                codec_type=ContentType.ALAC,
                sample_rate=sample_rate,
                bit_depth=bit_depth,
            ),
            can_seek=True,
            allow_seek=True,
            verify_ssl=False,
            expiration=3600,
            data={"lossless_token": token},
        )

    async def _get_alac_variant(self, item_id: str) -> tuple[tuple[int, int], str] | None:
        """Return the best ALAC quality and its media playlist URL."""
        response = await self._get_lossless_attributes(item_id)
        data = (response or {}).get("data") or []
        if not data:
            return None
        attributes = data[0].get("attributes") or {}
        traits = set(attributes.get("audioTraits") or [])
        self.logger.debug("Apple Music lossless traits for %s: %s", item_id, traits)
        if not traits.intersection({"lossless", "hi-res-lossless"}):
            return None
        master_url = (attributes.get("extendedAssetUrls") or {}).get("enhancedHls")
        if not master_url:
            return None
        master = await self._fetch_text(master_url)
        variant = self._parse_alac_variant(master_url, master)
        self.logger.debug("Apple Music lossless variant for %s: %s", item_id, variant)
        return variant

    async def _get_lossless_attributes(self, item_id: str) -> dict[str, Any] | None:
        """Read extended song attributes from the web API that exposes audioTraits."""
        token = await self._get_web_token()
        if not token:
            return None
        headers = {
            "authorization": f"Bearer {token}",
            "origin": "https://music.apple.com",
            "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        }
        async with self.provider.mass.http_session.get(
            f"https://amp-api.music.apple.com/v1/catalog/"
            f"{self.provider._storefront or 'us'}/songs/{item_id}",
            params={"extend": "extendedAssetUrls"},
            headers=headers,
            ssl=True,
            timeout=ClientTimeout(total=20),
        ) as response:
            response.raise_for_status()
            return cast("dict[str, Any]", await response.json(loads=json_loads))

    async def _get_web_token(self) -> str | None:
        """Return a short-lived web-player token accepted by amp-api."""
        if self._web_token and time.monotonic() < self._web_token_expires:
            return self._web_token
        try:
            home = await self._fetch_text("https://music.apple.com/us/")
            js_path = re.search(r"/assets/index~[^/]+\.js", home)
            if not js_path:
                return None
            js = await self._fetch_text(urljoin("https://music.apple.com/us/", js_path.group(0)))
            token = re.search(r"eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+", js)
            if not token:
                return None
            self._web_token = token.group(0)
            self._web_token_expires = time.monotonic() + 3600
            return self._web_token
        except ClientError, TimeoutError, ValueError:
            return None

    @classmethod
    def _parse_alac_variant(
        cls, master_url: str, master: str
    ) -> tuple[tuple[int, int], str] | None:
        """Parse ALAC qualities and their media playlist URLs from an HLS master."""
        media: dict[str, tuple[int, int]] = {}
        variants: dict[str, str] = {}
        pending_group: str | None = None
        for raw_line in master.splitlines():
            line = raw_line.strip()
            if line.startswith("#EXT-X-MEDIA:"):
                attrs = cls._parse_attributes(line)
                group = attrs.get("GROUP-ID")
                if attrs.get("TYPE") != "AUDIO" or not group:
                    continue
                try:
                    media[group] = (int(attrs["SAMPLE-RATE"]), int(attrs["BIT-DEPTH"]))
                except KeyError, ValueError:
                    continue
            elif line.startswith("#EXT-X-STREAM-INF:"):
                attrs = cls._parse_attributes(line)
                group = attrs.get("AUDIO")
                pending_group = (
                    group if group and "alac" in attrs.get("CODECS", "").lower() else None
                )
            elif line and not line.startswith("#") and pending_group:
                variants[pending_group] = urljoin(master_url, line)
                pending_group = None
        candidates = [
            (media[group], variants[group])
            for group in media
            if group in variants and "alac" in group.lower()
        ]
        return max(candidates, key=lambda item: item[0]) if candidates else None

    async def _rewrite_playlist(
        self, item_id: str, media_url: str, playlist: str
    ) -> tuple[str, set[str]] | None:
        """Replace Apple key URIs with local key URLs and return the rewritten playlist."""
        lines = playlist.splitlines()
        output: list[str] = []
        key_block: list[str] = []
        key_tokens: set[str] = set()
        for line in lines:
            if line.startswith("#EXT-X-KEY:"):
                key_block.append(line)
                continue
            if key_block:
                rewritten = await self._rewrite_key_block(item_id, key_block)
                if rewritten is None:
                    return None
                key_line, key_token = rewritten
                output.append(key_line)
                key_tokens.add(key_token)
                key_block = []
            output_line = line
            if line.startswith("#EXT-X-MAP:"):
                map_uri = self._parse_attributes(line).get("URI")
                if map_uri:
                    output_line = line.replace(
                        f'URI="{map_uri}"', f'URI="{urljoin(media_url, map_uri)}"', 1
                    )
            elif line and not line.startswith("#"):
                output_line = urljoin(media_url, line)
            output.append(output_line)
        if key_block:
            rewritten = await self._rewrite_key_block(item_id, key_block)
            if rewritten is None:
                return None
            key_line, key_token = rewritten
            output.append(key_line)
            key_tokens.add(key_token)
        return "\n".join(output) + "\n", key_tokens

    async def _rewrite_key_block(
        self, item_id: str, key_block: list[str]
    ) -> tuple[str, str] | None:
        """Obtain one Widevine key and replace a group of EXT-X-KEY tags with it."""
        skd_uri: str | None = None
        pssh_b64: str | None = None
        for line in key_block:
            attrs = self._parse_attributes(line)
            uri = attrs.get("URI") or ""
            key_format = attrs.get("KEYFORMAT", "").lower()
            if uri.startswith("skd://"):
                skd_uri = uri
            elif uri.startswith("data:") and ("edef8ba9" in key_format or "widevine" in key_format):
                pssh_b64 = uri.split(",", 1)[1] if "," in uri else None
        if not skd_uri or not pssh_b64:
            return None
        key = await self._get_alac_key(item_id, skd_uri, pssh_b64)
        key_token = str(uuid())
        self._keys[key_token] = (time.time() + _ASSET_TTL, key)
        key_url = self._asset_url("key", key_token)
        key_line = (
            f'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="{key_url}",'
            'KEYFORMAT="com.apple.streamingkeydelivery",KEYFORMATVERSIONS="1"'
        )
        return key_line, key_token

    async def _get_alac_key(self, item_id: str, skd_uri: str, pssh_b64: str) -> bytes:
        """Acquire and cache a Widevine content key for one FairPlay key URI."""
        cache_key = (item_id, skd_uri, pssh_b64)
        if cached := self._key_cache.get(cache_key):
            return cached
        if not self.cdm_available:
            raise MediaNotFoundError("Widevine CDM files are not available")
        try:
            pssh = PSSH(pssh_b64)
        except (ValueError, TypeError) as err:
            raise MediaNotFoundError(f"Could not parse Widevine PSSH: {err}") from err
        device = Device(
            client_id=self._decrypt_client_id,
            private_key=self._decrypt_private_key,
            type_=DeviceTypes.ANDROID,
            security_level=3,
            flags={},
        )
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        try:
            challenge = cdm.get_license_challenge(session_id, pssh)
            adam_id = "0" if "P000000000" in skd_uri else item_id
            license_payloads: list[str] = []
            if pssh.key_ids:
                kid = pssh.key_ids[0]
                kid_bytes = kid.bytes if hasattr(kid, "bytes") else bytes(kid)
                kid_b64 = base64.b64encode(kid_bytes).decode("utf-8")
                license_payloads.append(f"{skd_uri},{kid_b64}")
            license_payloads.extend((f"{skd_uri},{pssh_b64}", pssh_b64))
            last_error: Exception | None = None
            for license_url in await self._license_urls(item_id):
                for uri in license_payloads:
                    try:
                        license_b64 = await self._request_license(
                            challenge, license_url, uri, adam_id
                        )
                        cdm.parse_license(session_id, license_b64)
                        key = next(
                            (key for key in cdm.get_keys(session_id) if key.type == "CONTENT"),
                            None,
                        )
                        if key is None:
                            raise MediaNotFoundError(
                                "License returned no content key"
                            )
                        key_bytes = key.key
                        self._key_cache[cache_key] = key_bytes
                        return key_bytes
                    except (ClientError, MediaNotFoundError, TimeoutError, ValueError) as err:
                        last_error = err
            raise MediaNotFoundError(f"No usable ALAC license: {last_error}")
        finally:
            cdm.close(session_id)

    async def _license_urls(self, item_id: str) -> tuple[str, ...]:
        """Return the license endpoints to try, fixed endpoint first."""
        if cached := self._license_urls_cache.get(item_id):
            return cached
        urls = [_LICENSE_URL]
        try:
            metadata = await self._fetch_song_stream_metadata(item_id)
            if webplayback_url := metadata.get("hls-key-server-url"):
                urls.append(str(webplayback_url))
        except ClientError, MediaNotFoundError, TimeoutError, KeyError, TypeError:
            pass
        result = tuple(dict.fromkeys(urls))
        self._license_urls_cache[item_id] = result
        return result

    async def _request_license(
        self, challenge: bytes, license_url: str, uri: str, adam_id: str
    ) -> str:
        """Post a Widevine challenge to Apple and return the license payload."""
        headers = dict(self._decryption_headers)
        if self.provider._music_user_token:
            headers["x-apple-music-user-token"] = self.provider._music_user_token
        payload = {
            "challenge": base64.b64encode(challenge).decode("utf-8"),
            "key-system": "com.widevine.alpha",
            "uri": uri,
            "adamId": adam_id,
            "isLibrary": False,
            "user-initiated": True,
        }
        self.logger.debug(
            "Apple Music ALAC license attempt url=%s uri_prefix=%s adam_id=%s",
            license_url,
            uri[:120],
            adam_id,
        )
        async with self.provider.mass.http_session.post(
            license_url,
            data=json_dumps(payload),
            headers=headers,
            ssl=False,
            timeout=ClientTimeout(total=30),
        ) as response:
            raw = await response.text()
            self.logger.debug(
                "Apple Music ALAC license response status=%s body=%s",
                response.status,
                raw[:600],
            )
            response.raise_for_status()
            content = json_loads(raw)
        license_b64 = content.get("license")
        if not license_b64:
            raise MediaNotFoundError(
                "Apple returned no ALAC license "
                f"(errorCode={content.get('errorCode')!r}, status={content.get('status')!r})"
            )
        return cast("str", license_b64)

    async def _fetch_text(self, url: str) -> str:
        """Fetch an HLS playlist."""
        async with self.provider.mass.http_session.get(
            url, ssl=True, timeout=ClientTimeout(total=15)
        ) as response:
            response.raise_for_status()
            return await response.text()

    def _asset_url(self, kind: str, token: str) -> str:
        """Return the local URL for a playlist or key asset."""
        base_url = self.provider.mass.webserver.internal_base_url.rstrip("/")
        if not base_url:
            raise MediaNotFoundError("MA webserver has no internal URL")
        return f"{base_url}{self._route_prefix}/{kind}?token={token}"

    async def _handle_route(self, request: web.Request) -> web.Response:
        """Serve a rewritten playlist or a decryption key to ffmpeg."""
        token = request.query.get("token", "")
        self._prune_assets()
        if request.path.endswith("/key"):
            item = self._keys.get(token)
            if item is None:
                return web.Response(status=404, text="key not found")
            return web.Response(
                body=item[1],
                content_type="application/octet-stream",
                headers={"Cache-Control": "no-store"},
            )
        item = self._playlists.get(token)
        if item is None:
            return web.Response(status=404, text="playlist not found")
        return web.Response(
            text=item[1],
            content_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )

    def _prune_assets(self) -> None:
        """Drop expired playlists and keys."""
        now = time.time()
        self._playlists = {key: value for key, value in self._playlists.items() if value[0] > now}
        self._keys = {key: value for key, value in self._keys.items() if value[0] > now}

    @staticmethod
    def _parse_attributes(line: str) -> dict[str, str]:
        """Parse key=value attributes from an HLS tag."""
        return {
            match.group(1): match.group(2).strip('"') for match in _HLS_ATTRIBUTE_RE.finditer(line)
        }
