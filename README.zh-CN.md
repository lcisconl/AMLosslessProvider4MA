# 适用于 Music Assistant 的 Apple Music Lossless

[English](README.md) | 简体中文

一个实验性的 [Music Assistant](https://music-assistant.io/) 音乐 provider，
可通过 **ALAC 无损** 播放 Apple Music。目录、搜索、音乐库和播放列表逻辑
继承自内置的 Apple Music provider；仅替换串流解析，使用外部的 FairPlay
网关。

> FairPlay 运行时是独立服务，**不在**本仓库中：
> [lcisconl/apple-fairplay](https://github.com/lcisconl/apple-fairplay)

## 功能

- 通过 Apple 增强 HLS 源码播放 ALAC 无损，包括 Hi-Res
- 通过本地 FairPlay sidecar 播放
- 无损不可用时自动回退到 Widevine 增强 HLS，再回退到常规 AAC 串流
- 磁盘歌曲缓存：播放时边听边写，之后直接作为本地文件播放，不再创建
  FairPlay 会话，也不再访问网关
- LRU 淘汰、可配置容量上限、启用/禁用与环境变量覆盖
- Provider 诊断信息包含 CDM、路由与缓存状态
- 支持多实例

## 工作原理

1. 对于一首歌曲，provider 会向 FairPlay 网关查询能力并创建播放会话
   （`/api/v1/capabilities`、`/api/v1/playback`）。
2. 从网关串流 ALAC 数据（`/api/v1/playback/<session>/audio`）。网关支持时
   请求 Hi-Res。
3. 如果网关被禁用、不可用或拒绝该歌曲，provider 会尝试 Widevine 增强 HLS
   路径；如果该播放列表没有可用的密钥材料，则回退到基础 Apple Music 的
   AAC 串流。
4. 播放过程中会把副本写入本地歌曲缓存。下次播放直接以 `LOCAL_FILE` 流从
   该文件提供，支持原生 seek，且不需要 FairPlay 会话。

## 环境要求

- 运行本 provider 的 Music Assistant 服务器
- 从 Music Assistant 可访问的 FairPlay 引擎，默认地址为
  `https://127.0.0.1:20025`
- `pywidevine==1.9.0`（在 `manifest.json` 中声明，用于 Widevine 回退路径）
- Apple Music 订阅，且已完成 Apple Music 登录
- Music Assistant 与网关共享的 FairPlay API Token

## 安装配置

### 1. 启动 FairPlay 引擎

两个服务通常使用 host 网络部署，使 Music Assistant 可以通过回环地址访问
引擎。引擎监听：

```text
https://127.0.0.1:20025
```

端口 `20025` 不应暴露到局域网。在引擎容器中完成一次 Apple 登录，并确保
会话数据持久化：

```bash
docker compose exec -it apple-fairplay \
  python3 /app/apple_fairplay/login.py
```

### 2. 在 Music Assistant 中添加 provider

添加 **Apple Music Lossless** provider。设置向导第一步会要求：

```text
FairPlay Gateway URL
FairPlay API Token
```

使用 `https://127.0.0.1:20025`，并填写与引擎上
`APPLE_MUSIC_FAIRPLAY_TOKEN` 相同的 token。这些值保存在 provider 配置中；
Music Assistant 服务本身不需要任何 FairPlay 环境变量。

### 3. 登录 Apple Music

继续设置向导，在浏览器窗口中用 Apple ID 登录。如果内置的 MusicKit
developer token 不可用或已过期，向导会要求输入有效的 developer token。
高级用户也可以直接粘贴 Music User Token，例如用于儿童账号。

### 4. 播放器访问地址

如果播放器位于局域网，请设置 Music Assistant 的发布地址和 base URL，例如：

```text
publish_ip: 192.168.1.10
base_url:   http://192.168.1.10:8095
```

不要在面向播放器的 URL 中使用 `127.0.0.1`。

## 配置

### Provider 设置

| 设置 | 键 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 启用歌曲缓存 | `cache_enabled` | `true` | 将解密后的歌曲缓存到磁盘。关闭时会删除已有缓存。 |
| 歌曲缓存大小 (MB) | `cache_max_size_mb` | `4096` | 容量上限；最久未播放的歌曲优先淘汰。设为 `0` 会禁用缓存并删除已有缓存。 |
| 歌曲缓存目录 | `cache_dir` | 空 | 可选目录。留空则使用 Music Assistant 的数据/缓存目录。会自动追加 provider 实例 ID。 |

修改这些设置需要重载 provider。FairPlay 网关地址和 token 在设置向导中配置。

### 环境变量

环境变量优先于 provider 设置，在容器部署中很方便：

```text
APPLE_MUSIC_LOSSLESS_CACHE=0          # 禁用缓存（并清空）
APPLE_MUSIC_LOSSLESS_CACHE_MAX_MB=8192
APPLE_MUSIC_LOSSLESS_CACHE_DIR=/cache/apple-music
```

## 歌曲缓存

- 默认位置：
  `<Music Assistant data/cache>/apple_music_lossless/<provider instance id>`
- 文件名为 `<歌曲 ID>.alac.<采样率>.<位深>.mp4`。
- 播放时先写入临时 `.part` 文件，串流完整结束后才原子提交。未完成、被取消
  或超过容量限制的歌曲不会被缓存。
- 缓存命中时以 `StreamType.LOCAL_FILE` 提供，因此 seek、暂停和重播都无需
  FairPlay 网关。
- 关闭缓存、把大小设为 `0`，或设置 `APPLE_MUSIC_LOSSLESS_CACHE=0`，会在
  下次 provider 启动时删除已有缓存。重新启用后从空缓存开始。
- 更换缓存目录不会移动或删除旧目录中的文件。
- 缓存命中、未命中、淘汰次数、文件数量和磁盘占用都会包含在 provider 诊断
  信息中。

## 诊断

Provider 诊断信息包含：

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

`cdm_available` 只影响 Widevine 回退路径；FairPlay 播放不依赖它。

## 常见问题

- **总是回退到 AAC**：检查 Music Assistant 日志中的
  `Apple Music lossless unavailable ... falling back to AAC`。确认网关在
  配置的 URL 上可访问、API token 一致，并且网关有有效的 Apple 会话。网关
  响应会在 debug 级别记录。
- **`No FairPlay playback session`**：串流详情已过期，或网关拒绝了该会话。
  重新开始播放以重新解析串流详情。
- **没有使用 Hi-Res**：歌曲或账号必须支持 Hi-Res 版本，并且网关必须报告
  `hiRes` 支持。provider 会记录协商后的质量。
- **缓存不增长**：确认缓存已启用、容量上限大于单曲大小，并且 Music
  Assistant 对缓存目录有写权限。
- **清空缓存**：关闭缓存（或将大小设为 `0`）并重载 provider，或手动删除
  实例目录。

## 仓库结构

```text
__init__.py         Provider 初始化入口
provider.py         Provider 类、配置、缓存接线
streaming.py        FairPlay/ALAC 解析、Widevine HLS 回退
cache.py            磁盘 LRU 歌曲缓存
setup_flow.py       FairPlay + Apple Music 登录设置向导
constants.py        配置键与默认值
manifest.json       Music Assistant provider manifest
strings.json        UI 文案
icon.svg            Provider 图标
icon_monochrome.svg 单色 Provider 图标
LICENSE             MIT 许可证
```

## 说明

- 本 provider 为实验性、非官方项目，与 Apple 无隶属关系，也未获 Apple 认可。
- 无损播放仅适用于你自己的 Apple Music 订阅有权访问的内容。请遵守 Apple
  服务条款和当地法律。
- FairPlay 引擎是独立项目，不包含在本仓库中。

## 许可证

本项目基于 [MIT 许可证](LICENSE) 发布。

## 致谢

- 基于 Music Assistant 的 Apple Music provider 构建
- [pywidevine](https://github.com/devine-dl/pywidevine) 用于 Widevine
  回退路径
- [lcisconl/apple-fairplay](https://github.com/lcisconl/apple-fairplay) 提供
  FairPlay 运行时
