# Apple Music Lossless for Music Assistant

An experimental [Music Assistant](https://music-assistant.io/) music provider
that plays Apple Music in **ALAC lossless**. Catalogue, search, library and
playlist handling are inherited from the built-in Apple Music provider; only
stream resolution is replaced, using an external FairPlay gateway.

> The FairPlay runtime is a separate service and is **not** part of this
> repository: [lcisconl/apple-fairplay](https://github.com/lcisconl/apple-fairplay)

## Images

| Service | Image |
| --- | --- |
| Music Assistant with this provider | `ghcr.io/lcisconl/music-assistant-apple-lossless:latest` |
| FairPlay engine | `ghcr.io/lcisconl/apple-fairplay:latest` |

## Features

- ALAC lossless playback, including hi-res, from Apple's enhanced HLS source
- FairPlay gateway playback through a local sidecar
- Automatic fallback to the Widevine enhanced-HLS path, then to the regular
  AAC stream, when lossless playback is not available
- On-disk track cache: tracks are cached while they stream and replayed as
  local files, without opening a new FairPlay session or contacting the gateway
- LRU cache eviction, configurable size limit, enable/disable and environment
  overrides
- Provider diagnostics with CDM, route and cache state
- Multi-instance support

## How it works

1. For a track, the provider asks the FairPlay gateway for its capabilities and
   opens a playback session (`/api/v1/capabilities`, `/api/v1/playback`).
2. ALAC bytes are streamed from the gateway (`/api/v1/playback/<session>/audio`).
   Hi-res is requested when the gateway supports it.
3. If the gateway is disabled, unavailable, or refuses the track, the provider
   tries the Widevine enhanced-HLS path. If that playlist has no usable key
   material, resolution falls back to the base Apple Music AAC stream.
4. While a track streams, a copy is written to the local track cache. The next
   play is served directly from that file as a `LOCAL_FILE` stream, with native
   seeking and no FairPlay session.

## Requirements

- Music Assistant server running this provider (see the image above)
- A FairPlay engine reachable from Music Assistant, by default at
  `https://127.0.0.1:20025`
- `pywidevine==1.9.0` (declared in `manifest.json`, used by the Widevine
  fallback path)
- An Apple Music subscription and a completed Apple Music sign-in
- The FairPlay API token shared between Music Assistant and the gateway

## Setup

### 1. Start the FairPlay engine

Both services are normally deployed with host networking so Music Assistant can
reach the engine on the loopback address. The engine listens on:

```text
https://127.0.0.1:20025
```

Port `20025` should not be published to the LAN. Sign in to Apple once in the
engine container and make sure its session data is persisted:

```bash
docker compose exec -it apple-fairplay \
  python3 /app/apple_fairplay/login.py
```

### 2. Add the provider in Music Assistant

Add the **Apple Music Lossless** provider. The first setup step asks for:

```text
FairPlay Gateway URL
FairPlay API Token
```

Use `https://127.0.0.1:20025` and the same token as
`APPLE_MUSIC_FAIRPLAY_TOKEN` on the engine. These values are stored in the
provider configuration; the Music Assistant service does not need any FairPlay
environment variables.

### 3. Sign in to Apple Music

Continue the setup flow and sign in with your Apple ID in the browser window.
If the bundled MusicKit developer token is unavailable or expired, the flow
asks for a valid developer token. Advanced users can paste a Music User Token
instead, for example for child accounts.

### 4. Player-facing URLs

If players are on the LAN, set the Music Assistant published address and base
URL, for example:

```text
publish_ip: 192.168.1.10
base_url:   http://192.168.1.10:8095
```

Do not use `127.0.0.1` for player-facing URLs.

## Configuration

### Provider settings

| Setting | Key | Default | Description |
| --- | --- | --- | --- |
| Enable track cache | `cache_enabled` | `true` | Cache decrypted tracks on disk. Turning it off removes existing cached tracks. |
| Track cache size (MB) | `cache_max_size_mb` | `4096` | Size limit; least recently played tracks are evicted first. `0` disables the cache and removes cached tracks. |
| Track cache directory | `cache_dir` | empty | Optional directory. Empty uses the Music Assistant data/cache directory. The provider instance id is appended automatically. |

Changing these settings requires a provider reload. The FairPlay gateway URL and
token are configured in the setup flow.

### Environment variables

Environment variables take precedence over provider settings and are useful in
containers:

```text
APPLE_MUSIC_LOSSLESS_CACHE=0          # disable the cache (and purge it)
APPLE_MUSIC_LOSSLESS_CACHE_MAX_MB=8192
APPLE_MUSIC_LOSSLESS_CACHE_DIR=/cache/apple-music
```

## Track cache

- Default location:
  `<Music Assistant data/cache>/apple_music_lossless/<provider instance id>`
- Files are named `<song id>.alac.<sample rate>.<bit depth>.mp4`.
- A track is written to a temporary `.part` file while playing and committed
  atomically only after the stream completed. Incomplete streams, cancelled
  plays and oversized tracks are not cached.
- Cache hits are served as `StreamType.LOCAL_FILE`, so seek, pause and replay
  work without the FairPlay gateway.
- Turning the cache off, setting its size to `0`, or setting
  `APPLE_MUSIC_LOSSLESS_CACHE=0` removes existing cached tracks on the next
  provider start. Enabling it again starts with an empty cache.
- Changing the cache directory does not move or delete files in the old
  directory.
- Cache hits, misses, evictions, file count and disk usage are included in the
  provider diagnostics.

## Diagnostics

The provider diagnostics include:

```json
{
  "lossless": {
    "cdm_available": true,
    "route": "/apple_music_lossless/<instance id>",
    "cache": {
      "enabled": true,
      "available": true,
      "path": "/data/cache/apple_music_lossless/...",
      "limit_mb": 4096.0,
      "size_mb": 0.0,
      "tracks": 0,
      "hits": 0,
      "misses": 0,
      "evictions": 0
    }
  }
}
```

`cdm_available` only affects the Widevine fallback path; FairPlay playback does
not depend on it.

## Troubleshooting

- **Always falls back to AAC**: check the Music Assistant log for
  `Apple Music lossless unavailable ... falling back to AAC`. Verify that the
  gateway is reachable at the configured URL, the API tokens match, and the
  gateway has a valid Apple session. Gateway responses are logged in debug
  level.
- **`No FairPlay playback session`**: the stream details are stale or the
  gateway refused the session. Restart playback so stream details are resolved
  again.
- **Hi-res not used**: the track or account must have a hi-res variant, and the
  gateway must report `hiRes` support. The provider logs the negotiated quality.
- **Cache does not grow**: confirm the cache is enabled, the size limit is
  greater than the single track size, and Music Assistant can write to the
  cache directory.
- **Clear the cache**: turn the cache off (or set its size to `0`) and reload
  the provider, or delete the instance directory manually.

## Repository layout

```text
__init__.py         Provider setup entry point
provider.py         Provider class, configuration, cache wiring
streaming.py        FairPlay/ALAC resolution, Widevine HLS fallback
cache.py            On-disk LRU track cache
setup_flow.py       FairPlay + Apple Music sign-in setup flow
constants.py        Configuration keys and defaults
manifest.json       Music Assistant provider manifest
strings.json        UI strings
icon.svg            Provider icon
icon_monochrome.svg Monochrome provider icon
```

## Notes

- This provider is experimental and unofficial. It is not affiliated with or
  endorsed by Apple.
- Lossless playback only works for content your own Apple Music subscription
  can access. Respect Apple's terms of service and local law.
- The FairPlay engine is a separate project and is not included here.
- No license file is included in this repository. Add one before redistributing
  the code if you intend to publish it under a specific license.

## Credits

- Built on the Music Assistant Apple Music provider
- [pywidevine](https://github.com/devine-dl/pywidevine) for the Widevine
  fallback path
- [lcisconl/apple-fairplay](https://github.com/lcisconl/apple-fairplay) for the
  FairPlay runtime
