"""
Configuration module — reads from environment variables.
Heroku pe config vars use karo. Local dev me .env mat use karo, directly env vars set karo.
"""
import os

API_ID = int(os.environ.get("API_ID", 14050586))
API_HASH = os.environ.get("API_HASH", "42a60d9c657b106370c79bb0a8ac560c")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 7074383232))
UPSTREAM_REPO = os.environ.get("UPSTREAM_REPO", "https://github.com/IamElite/ECB")
UPSTREAM_BRANCH = os.environ.get("UPSTREAM_BRANCH", "main")
