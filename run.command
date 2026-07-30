#!/bin/zsh
cd /Users/jhsy/wechat-chat-ocr || exit 1
exec /Users/jhsy/.conda/envs/wechat-ocr/bin/python -m wechat_archive.app
