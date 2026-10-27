#!/usr/bin/env python3
"""Quick verification of deployment state.

Checks:
1. Git has both zip files committed
2. Version string is updated
3. Health endpoint config is correct
"""
import sys
import os

# Add backend to path
backend_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, backend_dir)

def main():
    print("=== Local Verification ===\n")

    # 1. Check git has the zips
    import subprocess
    result = subprocess.run(
        ["git", "ls-tree", "-r", "HEAD", "--name-only"],
        cwd=backend_dir,
        capture_output=True,
        text=True,
    )
    files = result.stdout.strip().split("\n")
    has_db_zip = "data/sofascore.zip" in files
    has_images_zip = "output_26-27_images.zip" in files
    print(f"Git data/sofascore.zip committed: {'OK' if has_db_zip else 'MISSING'}")
    print(f"Git output_26-27_images.zip committed: {'OK' if has_images_zip else 'MISSING'}")

    # 2. Check version string
    with open(os.path.join(backend_dir, "server_fantasy.py"), "r", encoding="utf-8") as f:
        content = f.read()
    if "v2-debug-fix-images-bd" in content:
        print("Version string updated: OK")
    else:
        print("Version string updated: OLD (still v2-debug-fix)")

    # 3. Check config
    try:
        from fantasy_config import IMAGES_ROOT, SOFASCORE_API, SOFASCORE_DB_PATH
        print(f"\nIMAGES_ROOT: {IMAGES_ROOT}")
        print(f"IMAGES_ROOT.exists(): {IMAGES_ROOT.exists()}")
        print(f"SOFASCORE_API: {repr(SOFASCORE_API)} (empty = disabled)")
        print(f"SOFASCORE_DB_PATH: {SOFASCORE_DB_PATH}")
        print(f"SOFASCORE_DB_PATH.exists(): {SOFASCORE_DB_PATH.exists()}")
    except Exception as e:
        print(f"Config error: {e}")

    print("\n=== Next Steps ===")
    print("1. Commit and push the version update:")
    print("   cd backend && git add server_fantasy.py && git commit -m 'bump version to images-bd' && git push")
    print("2. On PythonAnywhere, run:")
    print("   git pull origin main")
    print("   git checkout -- data/sofascore.zip output_26-27_images.zip")
    print("   cd data && unzip -o sofascore.zip && rm sofascore.zip && cd ..")
    print("   unzip -o output_26-27_images.zip -d . && rm output_26-27_images.zip")
    print("3. Reload web app")
    print("4. Run: python _test_pa.py https://xuxillo.pythonanywhere.com")


if __name__ == "__main__":
    main()
