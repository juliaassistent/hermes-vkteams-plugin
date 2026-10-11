# Hermes VK Teams (Среда) Platform Plugin

VK Teams (Среда) gateway adapter for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Long-polling bot integration: receives messages via `events/get`, sends text with
format-styles (bold, italic, links, quotes, lists, code blocks), edits and deletes
messages, shows a typing indicator.

> Ported from the OpenClaw `vkteams` channel plugin. The OpenClaw plugin is
> untouched; this is an independent Python implementation of the same
> VK Teams Bot API integration.

## Features

- Long polling (`events/get` with `lastEventId`, automatic retry with backoff)
- Markdown → VK Teams format-styles converter (character offsets, Cyrillic-safe):
  bold, italic, underline, strikethrough, links, mentions, inline code, code
  blocks, ordered/unordered lists, quotes
- `edit_message` / `delete_message`
- Typing indicator (`chats/sendActions`, debounced)
- Own-message filtering (bot never reacts to itself)
- `get_chat_info` (chat name/type)
- Message splitting for long responses (4096 chars)
- Allowlist (`VKTEAMS_ALLOWED_USERS`) / allow-all (`VKTEAMS_ALLOW_ALL_USERS`)

## Requirements

- Python 3.10+
- `httpx`
- Hermes Agent with gateway support

## Install

```bash
# 1. Copy the plugin into the Hermes user plugins directory
mkdir -p ~/.hermes/plugins/vkteams
cp -r plugins/platforms/vkteams/* ~/.hermes/plugins/vkteams/

# 2. Set the bot token (VK Teams / ICQ New bot token)
export VKTEAMS_TOKEN="001.<app>.<key>:<botUserId>"

# 3. Enable the platform in Hermes config.yaml
platforms:
  vkteams:
    enabled: true
    extra:
      token: "${VKTEAMS_TOKEN}"   # or rely on the env var
      # api_base_url: "https://api.icq.net/bot/v1"   # optional override
      # poll_time: 30                                # optional
      # allow_from: "user1@domain,user2@domain"      # optional allowlist
      # allow_all: "false"                           # optional

# 4. Start the Hermes gateway
hermes gateway
```

The plugin is discovered automatically at gateway startup (the Hermes plugin
loader scans `~/.hermes/plugins/`).

## Configuration

| Env var | Description | Default |
|---|---|---|
| `VKTEAMS_TOKEN` | VK Teams bot token | — (required) |
| `VKTEAMS_API_BASE_URL` | Bot API base URL | `https://api.icq.net/bot/v1` |
| `VKTEAMS_ALLOWED_USERS` | Comma-separated allowed user IDs | — (all allowed) |
| `VKTEAMS_ALLOW_ALL_USERS` | `true` to allow any user | `false` |
| `VKTEAMS_HOME_CHANNEL` | Default chat for cron/notification delivery | — |
| `VKTEAMS_POLL_TIME` | Long-poll seconds per `events/get` | `30` |

## VK Teams Bot API notes

- Auth: `token` query parameter (URL-encoded; `:` → `%3A`).
- No webhooks — long polling only: `events/get?pollTime=<s>&lastEventId=<id>`.
- `messages/sendText` accepts a `format` JSON with grouped styles, e.g.
  `{"bold":[{"offset":0,"length":4}], "link":[{"offset":0,"length":2,"url":"..."}]}`.
  Offsets are **character** offsets, not bytes.

## License

MIT
