"""Constants for the experimental Apple Music Lossless provider."""

CONF_FAIRPLAY_GATEWAY = "fairplay_gateway"
CONF_FAIRPLAY_TOKEN = "fairplay_token"
DEFAULT_FAIRPLAY_GATEWAY = "https://127.0.0.1:20025"
DEFAULT_FAIRPLAY_TOKEN = "test"

CONF_CACHE_ENABLED = "cache_enabled"
CONF_CACHE_MAX_SIZE_MB = "cache_max_size_mb"
CONF_CACHE_DIR = "cache_dir"
DEFAULT_CACHE_ENABLED = True
DEFAULT_CACHE_MAX_SIZE_MB = 4096

# Environment overrides (useful in containers where the settings UI is not used).
CACHE_ENV_ENABLED = "APPLE_MUSIC_LOSSLESS_CACHE"
CACHE_ENV_MAX_MB = "APPLE_MUSIC_LOSSLESS_CACHE_MAX_MB"
CACHE_ENV_DIR = "APPLE_MUSIC_LOSSLESS_CACHE_DIR"
