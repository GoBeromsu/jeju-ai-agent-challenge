"""Build a self-contained Windows release in an explicitly chosen output directory.

Example (online build):
    python3 build/package_windows.py --output-dir /tmp/jeju-release-01

Example (offline rebuild from previously saved artifacts):
    python3 build/package_windows.py --output-dir /tmp/jeju-release-02 --offline \\
        --python-archive /path/python-3.12.10-embed-amd64.zip \\
        --wheelhouse /path/to/jeju-agent/wheels

The ZIP contains the official CPython 3.12.10 Windows amd64 embeddable runtime,
the resolved CPython 3.12 / win_amd64 wheels, and only the project paths below.
The build refuses to write inside dist/ or replace existing release artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
PY_VERSION = "3.12.10"
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
ZIP_NAME = "jeju-agent-submission.zip"

# Allowlist source files rather than copying broad developer directories such as
# build/, tests/, and tools/. The web asset directories are the runtime data,
# fonts, and locally vendored Three.js dependencies used by the app.
GUIDE_IMAGE_PATHS = (
    "docs/guide-img/00-home.png",
    "docs/guide-img/01-region-question.png",
    "docs/guide-img/02-preference-question.png",
    "docs/guide-img/03-a-results.png",
    "docs/guide-img/04-a-card-rank-2.png",
    "docs/guide-img/05-a-pin-rank-1.png",
)
GUIDE_IMAGE_LINK = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
COPY_FILES = (
    "app/__init__.py",
    "app/__main__.py",
    "app/agent.py",
    "app/agent_openai.py",
    "app/advisor.py",
    "app/config.py",
    "app/consultation.py",
    "app/context_boundary.py",
    "app/engine.py",
    "app/hub.py",
    "app/place.py",
    "app/server.py",
    "app/walk.py",
    "web/index.html",
    "web/main.js",
    "web/style.css",
    "web/terrain.js",
    "web/studio.html",
    "web/studio-client.js",
    "web/studio-kakao.js",
    "web/studio-ui.css",
    "web/studio-street.js",
    "web/studio-sources.txt",
    "web/studio-footprints.json",
    "docs/설치매뉴얼.md",
    "docs/데이터활용명세.md",
    "docs/agent-contract.md",
    "docs/프롬프트.md",
    "docs/심사위원_사용가이드.md",
    "docs/proposal/기획서.pdf",
    "README.md",
    ".env.example",
    "requirements.txt",
    "실행.bat",
    "자유대화끄고실행.bat",
    *GUIDE_IMAGE_PATHS,
)
COPY_DIRS = ("web/data", "web/fonts", "web/vendor")
OMIT_FILES = {"web/data/population_snapshot.json"}  # no approval is bundled with a source file
SKIP_NAMES = {"__pycache__", ".DS_Store"}
SECRET_KEYS = {"GEMINI_API_KEY", "HUB_PROJECT_KEY", "KAKAO_MAP_JS_KEY", "OPENAI_API_KEY", "OPENAI_PROJECT"}
REQUIRED_PACKAGE_FILES = (
    "app/server.py",
    "app/consultation.py",
    "app/context_boundary.py",
    "web/studio.html",
    "web/studio-client.js",
    "web/studio-kakao.js",
    "web/data/dongs.json",
    "web/data/meta.json",
    "web/fonts/NotoSansKR-VF-subset.woff2",
    "docs/설치매뉴얼.md",
    "docs/proposal/기획서.pdf",
    "docs/포함된_라이브러리.txt",
    "README.md",
    ".env.example",
    "requirements.txt",
    "실행.bat",
    "자유대화끄고실행.bat",
    "python/python.exe",
    "python/python312.dll",
    "python/python312.zip",
    "python/python312._pth",
)


def fetch(url: str, dest: Path) -> Path:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "jeju-agent-packager"})
        with urllib.request.urlopen(req, timeout=120) as r, dest.open("wb") as f:
            shutil.copyfileobj(r, f)
    return dest


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def data_receipt(data_root: Path) -> tuple[dict, int, str]:
    meta = json.loads((data_root / "meta.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256()
    files = sorted(path for path in data_root.rglob("*") if path.is_file())
    for path in files:
        relative = path.relative_to(data_root).as_posix().encode("utf-8")
        digest.update(relative + b"\0" + bytes.fromhex(sha256(path)))
    return meta, len(files), digest.hexdigest()


def copy_file(relative: str, out: Path) -> None:
    src = ROOT / relative
    if not src.is_file() or src.is_symlink():
        raise SystemExit(f"허용 목록 파일이 없거나 일반 파일이 아닙니다: {relative}")
    if relative == ".env.example":
        for line in src.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() in SECRET_KEYS and value.strip():
                raise SystemExit(f".env.example에 비밀값이 있습니다: {key.strip()}")
    dest = out / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)


def copy_dir(relative: str, out: Path) -> None:
    src = ROOT / relative
    if not src.is_dir() or src.is_symlink():
        raise SystemExit(f"허용 목록 폴더가 없거나 일반 폴더가 아닙니다: {relative}")
    for path in sorted(src.rglob("*")):
        if path.is_symlink():
            raise SystemExit(f"허용 목록 안에 심볼릭 링크가 있습니다: {path.relative_to(ROOT)}")
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel in OMIT_FILES or any(part in SKIP_NAMES or part.startswith(".") for part in path.relative_to(src).parts):
            continue
        if path.suffix == ".pyc":
            continue
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)


def guide_image_paths(markdown: str) -> list[str]:
    paths = []
    for link in GUIDE_IMAGE_LINK.findall(markdown):
        parsed = urlsplit(link)
        if parsed.scheme or parsed.netloc or link.startswith(("/", "\\")):
            continue
        relative = unquote(parsed.path)
        if not relative:
            continue
        parts = ["docs"]
        for part in PurePosixPath(relative).parts:
            if part in ("", "."):
                continue
            if part == "..":
                if len(parts) == 1:
                    raise SystemExit(f"심사위원 사용 가이드 이미지 경로가 docs 밖을 가리킵니다: {link}")
                parts.pop()
            else:
                parts.append(part)
        paths.append("/".join(parts))
    return paths


def validate_guide_images(root: Path) -> None:
    guide = root / "docs" / "심사위원_사용가이드.md"
    if not guide.is_file():
        raise SystemExit(f"심사위원 사용 가이드가 없습니다: {guide.relative_to(root)}")
    paths = guide_image_paths(guide.read_text(encoding="utf-8"))
    missing = [path for path in paths if not (root / path).is_file()]
    if missing:
        raise SystemExit(f"심사위원 사용 가이드의 상대 이미지 링크가 깨졌습니다: {missing[0]}")
    if len(paths) != len(GUIDE_IMAGE_PATHS) or set(paths) != set(GUIDE_IMAGE_PATHS):
        raise SystemExit(
            "심사위원 사용 가이드는 allowlist의 여섯 스크린샷만 참조해야 합니다: "
            + ", ".join(paths)
        )


def validate_source_allowlist(out: Path) -> None:
    exact_files = set(COPY_FILES) | {"docs/포함된_라이브러리.txt"}
    copied = {path.relative_to(out).as_posix() for path in out.rglob("*") if path.is_file()}
    unexpected = {
        relative for relative in copied
        if relative not in exact_files
        and not relative.startswith(("python/", "wheels/"))
        and not any(relative.startswith(f"{directory}/") for directory in COPY_DIRS)
    }
    if unexpected:
        raise SystemExit(f"허용 목록 밖 파일이 있습니다: {sorted(unexpected)[0]}")
    missing = [item for item in REQUIRED_PACKAGE_FILES if not (out / item).is_file()]
    if missing:
        raise SystemExit(f"필수 배포 파일이 없습니다: {', '.join(missing)}")
    if (out / ".env").exists() or any(out.rglob("*.pyc")):
        raise SystemExit("비밀 환경 파일 또는 Python 캐시가 배포 폴더에 있습니다")
    validate_guide_images(out)


def extract_python_archive(archive: Path, py: Path) -> Path:
    required = {"python.exe", "python312.dll", "python312.zip", "python312._pth"}
    with zipfile.ZipFile(archive) as source:
        names = set(source.namelist())
        if not required <= names:
            raise SystemExit(f"Python {PY_VERSION} Windows embeddable ZIP이 아닙니다: {archive}")
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise SystemExit(f"Python 런타임 ZIP에 안전하지 않은 경로가 있습니다: {name}")
        source.extractall(py)
    pth = py / "python312._pth"
    pth.write_text("python312.zip\n.\nLib\\site-packages\n..\nimport site\n", encoding="ascii")
    return pth


def download_wheels(wheels: Path, wheelhouse: Path | None, offline: bool) -> list[Path]:
    wheels.mkdir()
    command = [
        sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "--no-cache-dir",
        "-r", str(ROOT / "requirements.txt"), "-d", str(wheels), "--platform", "win_amd64",
        "--python-version", "312", "--implementation", "cp", "--abi", "cp312",
        "--only-binary=:all:",
    ]
    if wheelhouse:
        command.extend(["--find-links", str(wheelhouse)])
    if offline:
        command.append("--no-index")
    subprocess.run(command, check=True)
    result = sorted(wheels.glob("*.whl"))
    if not result:
        raise SystemExit("Windows용 휠을 받지 못했습니다")
    requested = {
        line.split("==", 1)[0].strip().lower().replace("_", "-")
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    found = {path.name.split("-", 1)[0].lower().replace("_", "-") for path in result}
    if not requested <= found:
        raise SystemExit(f"직접 의존 휠이 빠졌습니다: {', '.join(sorted(requested - found))}")
    return result


def install_wheels(wheels: list[Path], site: Path) -> list[str]:
    site.mkdir(parents=True, exist_ok=True)
    lines = []
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                member = PurePosixPath(name)
                if member.is_absolute() or ".." in member.parts:
                    raise SystemExit(f"휠에 안전하지 않은 경로가 있습니다: {wheel.name}:{name}")
            archive.extractall(site)
        lines.append(f"{wheel.name}  sha256={sha256(wheel)}")
    return lines


def write_zip(folder: Path, archive_path: Path) -> None:
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            name = (Path("jeju-agent") / path.relative_to(folder)).as_posix()
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def validate_zip(archive_path: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        bad = archive.testzip()
        if bad:
            raise SystemExit(f"ZIP 손상: {bad}")
        names = set(archive.namelist())
        if not all(f"jeju-agent/{path}" in names for path in REQUIRED_PACKAGE_FILES):
            raise SystemExit("ZIP에서 필수 배포 파일을 찾지 못했습니다")
        guide = archive.read("jeju-agent/docs/심사위원_사용가이드.md").decode("utf-8")
        guide_images = guide_image_paths(guide)
        if len(guide_images) != len(GUIDE_IMAGE_PATHS) or set(guide_images) != set(GUIDE_IMAGE_PATHS):
            raise SystemExit("ZIP 내 심사위원 사용 가이드의 이미지 링크가 여섯 이미지 allowlist와 다릅니다")
        missing_images = [path for path in guide_images if f"jeju-agent/{path}" not in names]
        if missing_images:
            raise SystemExit(f"ZIP 내 심사위원 사용 가이드 이미지가 없습니다: {missing_images[0]}")
        forbidden = [
            name for name in names
            if name.startswith(("jeju-agent/tests/", "jeju-agent/build/", "jeju-agent/tools/", "jeju-agent/logs/"))
            or name == "jeju-agent/.env"
            or name.endswith("/.env")
            or "__pycache__" in PurePosixPath(name).parts
            or name.endswith(".pyc")
        ]
        if forbidden:
            raise SystemExit(f"배포 제외 경로가 ZIP에 있습니다: {forbidden[0]}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="오프라인 Windows 포터블 ZIP 생성")
    parser.add_argument("--output-dir", type=Path, required=True, help="기존 dist/ 밖의 새 릴리스 출력 경로")
    parser.add_argument("--python-archive", type=Path, help="사전에 받은 공식 embeddable ZIP")
    parser.add_argument("--wheelhouse", type=Path, help="CPython 3.12 / win_amd64 호환 휠 폴더")
    parser.add_argument("--offline", action="store_true", help="Python ZIP과 wheelhouse만 사용, 네트워크 호출 금지")
    args = parser.parse_args()
    if args.offline and (not args.python_archive or not args.wheelhouse):
        parser.error("--offline 에는 --python-archive와 --wheelhouse가 모두 필요합니다")
    if args.wheelhouse:
        args.wheelhouse = args.wheelhouse.expanduser()
    if args.offline and (not args.wheelhouse.is_dir() or not any(args.wheelhouse.glob("*.whl"))):
        parser.error("--offline wheelhouse 폴더에 .whl 파일이 필요합니다")
    if args.wheelhouse and not args.wheelhouse.is_dir():
        parser.error("--wheelhouse 폴더가 없습니다")
    return args


def main() -> None:
    args = parse_args()
    out_dir = args.output_dir.expanduser().resolve()
    dist = (ROOT / "dist").resolve()
    if out_dir == dist or dist in out_dir.parents:
        raise SystemExit("안전을 위해 dist/ 안에는 빌드할 수 없습니다. 별도 출력 경로를 지정하세요.")
    out = out_dir / "jeju-agent"
    zip_path = out_dir / ZIP_NAME
    if out.exists() or out.is_symlink() or zip_path.exists() or zip_path.is_symlink():
        existing = out if out.exists() or out.is_symlink() else zip_path
        raise SystemExit(f"기존 산출물은 덮어쓰지 않습니다: {existing}")

    if args.python_archive and args.python_archive.name != Path(PY_URL).name:
        raise SystemExit(f"Python {PY_VERSION} 공식 embeddable ZIP 파일명이 필요합니다: {Path(PY_URL).name}")

    out_dir.mkdir(parents=True, exist_ok=True)
    out.mkdir()
    cache = out_dir / "cache"
    if args.python_archive:
        python_archive = args.python_archive.expanduser().resolve()
        if not python_archive.is_file():
            raise SystemExit(f"Python ZIP이 없습니다: {python_archive}")
    else:
        python_archive = fetch(PY_URL, cache / Path(PY_URL).name)
    if not zipfile.is_zipfile(python_archive):
        raise SystemExit(f"Python embeddable ZIP을 읽을 수 없습니다: {python_archive}")
    if (ROOT / "web/data/population_snapshot.json").is_file():
        print("출시 제외: web/data/population_snapshot.json (자료 승인 상태를 빌더가 확인할 수 없음)")

    py = out / "python"
    extract_python_archive(python_archive, py)
    wheels = download_wheels(
        out / "wheels",
        args.wheelhouse.expanduser().resolve() if args.wheelhouse else None,
        args.offline,
    )
    wheel_lines = install_wheels(wheels, py / "Lib" / "site-packages")

    for item in COPY_FILES:
        copy_file(item, out)
    for item in COPY_DIRS:
        copy_dir(item, out)
    data_meta, data_files, data_hash = data_receipt(out / "web" / "data")
    (out / "docs" / "포함된_라이브러리.txt").write_text(
        f"Python {PY_VERSION} embeddable (Windows amd64)\n"
        f"Source: {PY_URL}\n"
        f"Python ZIP sha256={sha256(python_archive)}\n"
        "Target: CPython 3.12 / win_amd64; wheels are included and extracted for offline use.\n"
        f"Store data date={data_meta.get('storeDate')}; records={data_meta.get('storeCount')}\n"
        f"Deployed web/data files={data_files} sha256={data_hash}\n"
        + "\n".join(wheel_lines) + "\n",
        encoding="utf-8",
    )
    validate_source_allowlist(out)
    write_zip(out, zip_path)
    validate_zip(zip_path)
    print(f"ZIP: {zip_path} ({zip_path.stat().st_size / 1_000_000:.1f} MB)")
    print(f"SHA-256: {sha256(zip_path)}")
    print(f"Wheels: {len(wheels)}; output files: {sum(1 for p in out.rglob('*') if p.is_file())}")


if __name__ == "__main__":
    main()
