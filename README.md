# Vivid Downloader Bot ⚡

A Telegram bot to download YouTube videos, automatically detect and record upcoming scheduled live streams, and lossless-split files larger than 2GB.

## Features
- YouTube video download with custom resolutions (480p, 720p, 1080p, Best).
- Auto-detection and scheduling of upcoming live streams.
- Lossless video splitting via FFmpeg for files exceeding 2GB.
- 24/7 web keep-alive server.

## Environment Variables
Set the following keys in your hosting platform (do not hardcode in repository):

| Variable | Description |
|---|---|
| `API_ID` | Telegram API ID from my.telegram.org |
| `API_HASH` | Telegram API Hash from my.telegram.org |
| `BOT_TOKEN` | Bot Token from @BotFather |
| `PORT` | Optional, defaults to `8080` |
