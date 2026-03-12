from pathlib import Path
import shutil


ROOT = Path(__file__).parent
STATIC_DIR = ROOT / 'static'
PUBLIC_STATIC_DIR = ROOT / 'public' / 'static'


def main():
    PUBLIC_STATIC_DIR.parent.mkdir(parents=True, exist_ok=True)
    if PUBLIC_STATIC_DIR.exists():
        shutil.rmtree(PUBLIC_STATIC_DIR)
    shutil.copytree(STATIC_DIR, PUBLIC_STATIC_DIR)
    print(f'Copied {STATIC_DIR} -> {PUBLIC_STATIC_DIR}')


if __name__ == '__main__':
    main()
