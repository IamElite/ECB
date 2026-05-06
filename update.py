import subprocess
import os
import sys
import tarfile
import tempfile
import json
import time

REPO_URL = "https://github.com/IamElite/ECB.git"

def run(cmd, check=True):
    print(f"\n>> {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    if check and result.returncode != 0:
        print(f"[FAIL] Code: {result.returncode}")
        sys.exit(1)
    return result

def main():
    print("=" * 40)
    print("ENCODER BOT AUTO DEPLOY")
    print("=" * 40)

    if not os.path.exists(".git"):
        run("git init")
        run(f"git remote add origin {REPO_URL}")

    run("git add .")
    run('git commit -m "auto: update deploy"')
    run("git branch -M main")
    run("git push -u origin main --force")

    print("\n" + "=" * 40)
    print("[OK] GitHub push done!")
    print("=" * 40)

if __name__ == "__main__":
    main()
