"""Безопасная распаковка архивов, собранных на Windows."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tarfile
import zipfile

CHUNK = 1024 * 1024
JUNK = {"__MACOSX", ".DS_Store", "Thumbs.db", "desktop.ini"}
SEVEN_ZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"
RAR_MAGIC = b"Rar!\x1a\x07"


class ArchiveError(Exception):
    """Сообщение этой ошибки показывается пользователю."""


class _Budget:
    """Ограничивает объём распаковки, чтобы архив не забил карту памяти."""

    def __init__(self, limit: int):
        self.left = limit

    def take(self, size: int) -> None:
        self.left -= size
        if self.left < 0:
            raise ArchiveError("Архив не помещается на диск малины.")


def _target(dest: str, raw: str) -> str | None:
    """Путь внутри dest или None для мусорных файлов. Выход за пределы dest — ошибка."""
    parts = [p for p in raw.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return None
    if ".." in parts or ":" in parts[0] or any("\0" in p for p in parts):
        raise ArchiveError(f"В архиве небезопасный путь: {raw!r}")
    if any(p in JUNK for p in parts):
        return None
    return os.path.join(dest, *parts)


def _copy(src, path: str, budget: _Budget) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as out:
        while True:
            chunk = src.read(CHUNK)
            if not chunk:
                return
            budget.take(len(chunk))
            out.write(chunk)


def _zip_name(info: zipfile.ZipInfo) -> str:
    """Проводник Windows пишет русские имена в cp866 без флага UTF-8."""
    if info.flag_bits & 0x800:
        return info.filename
    raw = info.filename.encode("cp437", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp866", "replace")


def _extract_zip(path: str, dest: str, budget: _Budget) -> None:
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.flag_bits & 0x1:
                raise ArchiveError("Архив защищён паролем. Упакуй его без пароля.")
            name = _zip_name(info)
            target = _target(dest, name)
            if target is None:
                continue
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                continue
            if name.endswith(("/", "\\")):
                os.makedirs(target, exist_ok=True)
                continue
            with zf.open(info) as src:
                _copy(src, target, budget)
            if mode & 0o111:
                os.chmod(target, 0o755)


def _extract_tar(path: str, dest: str, budget: _Budget) -> None:
    with tarfile.open(path, "r:*") as tf:
        for member in tf:
            target = _target(dest, member.name)
            if target is None:
                continue
            if member.isdir():
                os.makedirs(target, exist_ok=True)
            elif member.isreg():
                src = tf.extractfile(member)
                if src is not None:
                    with src:
                        _copy(src, target, budget)
                    if member.mode & 0o111:
                        os.chmod(target, 0o755)


def _extract_7z(path: str, dest: str, label: str) -> None:
    binary = shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
    if not binary:
        raise ArchiveError(f"Для {label} на малине нужен пакет p7zip-full. Или упакуй сайт в ZIP.")
    result = subprocess.run(
        [binary, "x", "-y", f"-o{dest}", path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=1800,
    )
    if result.returncode != 0:
        tail = result.stdout.decode("utf-8", "replace").strip()[-300:]
        raise ArchiveError(f"Не удалось распаковать {label}: {tail}. Попробуй упаковать сайт в ZIP.")
    # Внешняя утилита сама создаёт файлы, поэтому ссылки убираем после распаковки.
    for root, dirs, files in os.walk(dest):
        for name in dirs + files:
            full = os.path.join(root, name)
            if os.path.islink(full):
                os.unlink(full)
        dirs[:] = [d for d in dirs if d not in JUNK]
    for root, dirs, files in os.walk(dest, topdown=False):
        for name in files:
            if name in JUNK:
                os.unlink(os.path.join(root, name))
        for name in dirs:
            if name in JUNK:
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)


def _clean_tree(dest: str) -> None:
    """Убирает то, что собрано под Windows и на малине работать не будет."""
    for root, dirs, files in os.walk(dest):
        for name in list(dirs):
            full = os.path.join(root, name)
            is_venv = os.path.exists(os.path.join(full, "pyvenv.cfg"))
            is_node = name == "node_modules" and "package.json" in files
            if is_venv or is_node or name == "__pycache__":
                shutil.rmtree(full, ignore_errors=True)
                dirs.remove(name)


def _flatten(dest: str) -> None:
    """site.zip/site/index.html → index.html: снимает лишние обёртки-папки."""
    while True:
        entries = os.listdir(dest)
        if len(entries) != 1:
            return
        only = os.path.join(dest, entries[0])
        if not os.path.isdir(only):
            return
        holder = dest + ".unwrap"
        os.replace(only, holder)
        os.rmdir(dest)
        os.replace(holder, dest)


def _fix_scripts(dest: str) -> None:
    """Shell-скрипты с окончаниями строк CRLF на Linux не запускаются."""
    for root, dirs, files in os.walk(dest):
        if "node_modules" in dirs:
            dirs.remove("node_modules")
        for name in files:
            if not name.endswith(".sh"):
                continue
            full = os.path.join(root, name)
            if os.path.getsize(full) > CHUNK:
                continue
            with open(full, "rb") as f:
                data = f.read()
            if b"\r\n" in data:
                with open(full, "wb") as f:
                    f.write(data.replace(b"\r\n", b"\n"))


def extract(path: str, dest: str, limit: int) -> None:
    """Распаковывает архив в пустую папку dest. Формат определяет по содержимому."""
    os.makedirs(dest, exist_ok=True)
    budget = _Budget(limit)
    with open(path, "rb") as f:
        magic = f.read(8)
    try:
        if magic.startswith(SEVEN_ZIP_MAGIC):
            _extract_7z(path, dest, "7z")
        elif magic.startswith(RAR_MAGIC):
            _extract_7z(path, dest, "RAR")
        elif zipfile.is_zipfile(path):
            _extract_zip(path, dest, budget)
        elif tarfile.is_tarfile(path):
            _extract_tar(path, dest, budget)
        else:
            raise ArchiveError("Это не архив. Подходят ZIP, TAR, TAR.GZ, 7Z и RAR.")
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError) as exc:
        raise ArchiveError(f"Архив повреждён: {exc}") from exc

    _clean_tree(dest)
    if not os.listdir(dest):
        raise ArchiveError("Архив пустой.")
    _flatten(dest)
    _fix_scripts(dest)
