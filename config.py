"""
Configuration module — reads from Heroku env vars (production) or .env (local dev).
Local testing ke liye .env file banao, Heroku pe config vars use karo.
"""
import os
from dotenv import load_dotenv

# Load .env if it exists (local dev only — Heroku ignores .env)
load_dotenv()

API_ID = int(os.environ.get("API_ID", 14050586))
API_HASH = os.environ.get("API_HASH", "42a60d9c657b106370c79bb0a8ac560c")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8266949820:AAH5AZ58is4bI06UXmytuaSLIP0mKEnXMa8")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 7074383232))
UPSTREAM_REPO = os.environ.get("UPSTREAM_REPO", "https://github.com/IamElite/ECB")
UPSTREAM_BRANCH = os.environ.get("UPSTREAM_BRANCH", "main")
