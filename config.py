"""
Configuration module — reads from environment variables.
Heroku pe config vars use karo. Local dev me env vars set karo.
"""
import os

API_ID = int(os.environ.get("API_ID", 14050586))
API_HASH = os.environ.get("API_HASH", "42a60d9c657b106370c79bb0a8ac560c")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8266949820:AAH5AZ58is4bI06UXmytuaSLIP0mKEnXMa8")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 7074383232))
UPSTREAM_REPO = os.environ.get("UPSTREAM_REPO", "https://github.com/IamElite/ECB")
UPSTREAM_BRANCH = os.environ.get("UPSTREAM_BRANCH", "main")
AUTH_GC = int(os.environ.get("AUTH_GC", -1003192464251))

MAX_BOT_TASKS = int(os.environ.get("MAX_BOT_TASKS", 20))

_MAX_USER_TASKS = os.environ.get("MAX_USER_TASKS", None)
MAX_USER_TASKS = int(_MAX_USER_TASKS) if _MAX_USER_TASKS is not None else None
