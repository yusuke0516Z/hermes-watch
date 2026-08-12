#!/bin/zsh
# launchd から5分ごとに呼ばれるラッパー
cd "$(dirname "$0")"
exec /usr/bin/python3 hermes_monitor.py --once
