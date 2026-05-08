import os


class Config:
    API_ID = int(os.environ.get("API_ID", 14050586))
    API_HASH = os.environ.get("API_HASH", "42a60d9c657b106370c79bb0a8ac560c")
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "8266949820:AAH5AZ58is4bI06UXmytuaSLIP0mKEnXMa8")
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 7074383232))
    AUTH_GC = int(os.environ.get("AUTH_GC", -1003192464251))

    UPSTREAM_REPO = os.environ.get("UPSTREAM_REPO", "https://github.com/IamElite/ECB")
    UPSTREAM_BRANCH = os.environ.get("UPSTREAM_BRANCH", "main")

    MAX_BOT_TASKS = int(os.environ.get("MAX_BOT_TASKS", 20))
    MAX_USER_TASKS = int(v) if (v := os.environ.get("MAX_USER_TASKS", "")) else None
    DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads/")

    FFMPEG_CMDS = {
        "480p": '-i {input} -vf scale=-2:480 -c:v libx264 -preset medium -crf 29 -c:a aac -b:a 64k -sn {output}',
        "720p": '-i {input} -vf scale=-2:720 -c:v libx264 -preset medium -crf 27 -c:a aac -b:a 96k -sn {output}',
        "1080p": '-i {input} -vf scale=-2:1080 -c:v libx264 -preset medium -crf 25 -c:a aac -b:a 128k -sn {output}',
    }
