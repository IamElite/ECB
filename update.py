import os
import sys
import zipfile
import shutil
import tempfile
import subprocess
from urllib import request

UPSTREAM_REPO = os.environ.get("UPSTREAM_REPO", "https://github.com/IamElite/ECB")
UPSTREAM_BRANCH = os.environ.get("UPSTREAM_BRANCH", "main")

def update_from_repo():
    print("[UPDATE] Checking for updates...")

    clean_repo = UPSTREAM_REPO.rstrip("/")
    if clean_repo.endswith(".git"):
        clean_repo = clean_repo[:-4]

    zip_url = f"{clean_repo}/archive/refs/heads/{UPSTREAM_BRANCH}.zip"
    print(f"[UPDATE] Downloading from: {zip_url}")

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "update.zip")
        extract_path = os.path.join(tmp, "extracted")
        os.makedirs(extract_path)

        try:
            request.urlretrieve(zip_url, zip_path)
        except Exception as e:
            print(f"[UPDATE FAIL] {e}")
            return False

        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(extract_path)

        root_folder = os.listdir(extract_path)[0]
        root_path = os.path.join(extract_path, root_folder)

        skip = {".git", "__pycache__", ".env"}

        for item in os.listdir(root_path):
            if item in skip:
                continue
            s = os.path.join(root_path, item)
            d = os.path.join(".", item)
            if os.path.isdir(s):
                if os.path.exists(d):
                    shutil.rmtree(d)
                shutil.move(s, d)
            else:
                if os.path.exists(d):
                    os.remove(d)
                shutil.move(s, d)

    if os.path.exists("requirements.txt"):
        print("[UPDATE] requirements.txt found, installing dependencies...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "--quiet"])
        print("[UPDATE] Dependencies installed!")
    else:
        print("[UPDATE] No requirements.txt found, skipping pip install.")

    print("[UPDATE] Update applied successfully!")
    return True

update_from_repo()
