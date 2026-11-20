import os
import hashlib
import time
import shutil
import subprocess

try:
    import pwd
except ImportError:
    pwd = None


def file_hash(path):
    """Возвращает SHA-256 файла или None при ошибке."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def file_size(path):
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def format_bytes(size_bytes):
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def fix_posix_permissions(path):
    """Ensures the path and its contents are owned by the invoking user on POSIX when running via sudo."""
    if os.name != "posix" or os.getuid() != 0:
        return

    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")

    if sudo_uid and sudo_gid:
        try:
            subprocess.run(["chown", "-R", f"{sudo_uid}:{sudo_gid}", path], check=False, timeout=30)
        except Exception:
            pass


def get_posix_invoking_user_home():
    """Returns the invoking user's home on POSIX, even when running via sudo."""
    if os.name != "posix":
        return ""

    if pwd is not None:
        sudo_uid = os.environ.get("SUDO_UID")
        if sudo_uid:
            try:
                return pwd.getpwuid(int(sudo_uid)).pw_dir
            except Exception:
                pass

        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user:
            try:
                return pwd.getpwnam(sudo_user).pw_dir
            except Exception:
                pass

    home = os.environ.get("HOME")
    if home:
        return home

    return os.path.expanduser("~")


def backup_json_file(path):
    if not os.path.exists(path):
        return ""

    base = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    backup_path = base
    counter = 1
    while os.path.exists(backup_path):
        counter += 1
        backup_path = f"{base}-{counter}"

    shutil.copy2(path, backup_path)
    fix_posix_permissions(backup_path)
    return backup_path


def find_app_bundle(path):
    """Поднимается вверх от path до первой директории, оканчивающейся на .app.

    Используется только на macOS, чтобы определить корень .app-бандла
    для переподписи после модификации main.js.
    """
    p = os.path.abspath(path)
    while p and p != os.path.dirname(p):
        if p.endswith(".app"):
            return p
        p = os.path.dirname(p)
    return ""


def remove_macos_immutable_flags(path):
    """Снимает флаги uchg/schg с файла или директории на macOS.

    На macOS файлы внутри .app-бандлов могут иметь флаги immutable,
    которые блокируют запись даже для root (Errno 1 Operation not
    permitted при shutil.copy2 / создании бэкапа внутри бандла).
    Вызывать перед попыткой записи в .app-бандл.

    Возвращает True, если chflags отработал (или платформа не darwin).
    """
    import sys
    if sys.platform != "darwin":
        return True
    ok_flag = False
    # -R работает и для одиночных файлов, и для директорий.
    # Пробуем сначала связку nouchg,noschg, затем по отдельности
    # (noschg без Recovery может не сняться — это нормально).
    # Абсолютный путь /usr/bin/chflags: в frozen-сборках PATH может быть пустым.
    for args in (
        ["/usr/bin/chflags", "-R", "nouchg,noschg", path],
        ["/usr/bin/chflags", "-R", "nouchg", path],
        ["/usr/bin/chflags", "nouchg", path],
    ):
        try:
            res = subprocess.run(
                args,
                check=False, capture_output=True, timeout=30,
            )
            if res.returncode == 0:
                ok_flag = True
                break
        except FileNotFoundError:
            break
        except Exception:
            continue
    return ok_flag


def ensure_macos_writable(path):
    """Готовит путь внутри .app-бандла к записи на macOS.

    Снимает immutable-флаги с самого файла и с корня .app-бандла,
    а также quarantine с бандла. Безопасен на других ОС (no-op).
    Вызывать ПЕРЕД созданием бэкапа и ПЕРЕД атомарной заменой,
    т.к. обе операции пишут внутрь бандла.
    """
    import sys
    if sys.platform != "darwin":
        return
    try:
        if os.path.isfile(path) or os.path.isdir(path):
            remove_macos_immutable_flags(path)
    except Exception:
        pass
    try:
        app_path = find_app_bundle(path)
        if app_path and os.path.normcase(app_path) != os.path.normcase(path):
            remove_macos_immutable_flags(app_path)
            remove_macos_quarantine(app_path)
    except Exception:
        pass


def remove_macos_quarantine(path):
    """Снимает атрибут com.apple.quarantine с .app-бандла.

    Используется встроенная утилита /usr/bin/xattr — дополнительные
    Python-пакеты не нужны, работает и во frozen-сборках без os.*xattr API.
    """
    import sys
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["/usr/bin/xattr", "-dr", "com.apple.quarantine", path],
            check=False, capture_output=True, timeout=30,
        )
    except FileNotFoundError:
        pass
    except Exception:
        pass


def resign_macos_bundle(main_js_path):
    """Переподписывает .app ad-hoc подписью после изменения main.js.

    На macOS любая модификация файла внутри подписанного .app-бандла
    нарушает code signature. Electron-приложения с Hardened Runtime
    после этого падают при запуске. codesign --force --sign - кладёт
    ad-hoc подпись (без Developer ID), чего достаточно для локального
    запуска. Дополнительно снимается атрибут com.apple.quarantine,
    чтобы Gatekeeper не показывал предупреждение.
    """
    import sys
    if sys.platform != "darwin":
        return

    from patcher.utils.console import info, ok, warn

    app_path = find_app_bundle(main_js_path)
    if not app_path:
        # main.js лежит не внутри .app (например, portable-копия) — пропускаем
        return

    info(f"Re-signing {os.path.basename(app_path)} (ad-hoc)...")
    try:
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", app_path],
            check=True, capture_output=True, text=True, timeout=60,
        )
        ok("Ad-hoc signature applied")
    except FileNotFoundError:
        warn("codesign not found - install Xcode Command Line Tools")
        return
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        warn(f"codesign failed: {stderr}")
        return

    remove_macos_quarantine(app_path)


def resign_macos_binary(path):
    """Переподписывает Antigravity 2.0 бинарь ad-hoc подписью после его модификации.

    На Apple Silicon macOS немедленно SIGKILL-ит процесс, если code signature
    не совпадает с содержимым. codesign --force --sign - заменяет Developer ID
    подпись на ad-hoc (signing identity «-»), чего достаточно для локального запуска.
    """
    import sys
    if sys.platform != "darwin":
        return

    from patcher.utils.console import info, ok, warn

    real = os.path.realpath(path)
    info(f"Re-signing {os.path.basename(real)} (ad-hoc)...")
    try:
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--sign", "-", real],
            check=True, capture_output=True, text=True, timeout=60,
        )
        ok("Ad-hoc signature applied")
    except FileNotFoundError:
        warn("codesign not found — install Xcode Command Line Tools")
    except subprocess.CalledProcessError as e:
        warn(f"codesign failed: {(e.stderr or '').strip()}")


def get_executable_ranges(path: str) -> tuple[tuple[int, int], ...] | None:
    """Возвращает нормализованный кортеж смещений исполняемых секций ((start, end), ...) для PE/ELF файлов.
    Если файл неизвестного формата или произошла ошибка чтения, возвращает None (полный скан)."""
    import struct
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            if file_size < 0x100:
                return None
            f.seek(0)
            magic = f.read(4)
            if magic[:2] == b"MZ":
                f.seek(0x3C)
                pe_off = struct.unpack("<I", f.read(4))[0]
                if pe_off + 24 > file_size:
                    return None
                f.seek(pe_off)
                if f.read(4) != b"PE\0\0":
                    return None
                coff = f.read(20)
                sec_count = struct.unpack_from("<H", coff, 2)[0]
                opt_sz = struct.unpack_from("<H", coff, 16)[0]
                sec_table = pe_off + 24 + opt_sz
                ranges = []
                for i in range(sec_count):
                    f.seek(sec_table + i * 40)
                    sec = f.read(40)
                    if len(sec) < 40:
                        break
                    raw_size, raw_offset = struct.unpack_from("<II", sec, 16)
                    characteristics = struct.unpack_from("<I", sec, 36)[0]
                    # IMAGE_SCN_MEM_EXECUTE = 0x20000000
                    if characteristics & 0x20000000:
                        s = max(0, raw_offset)
                        e = min(file_size, raw_offset + raw_size)
                        if s < e:
                            ranges.append((s, e))
                if ranges:
                    ranges.sort()
                    merged = []
                    for s, e in ranges:
                        if merged and s <= merged[-1][1]:
                            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                        else:
                            merged.append((s, e))
                    return tuple(merged)
            elif magic == b"\x7fELF":
                f.seek(0)
                ident = f.read(16)
                if len(ident) >= 7:
                    elf_class, data_order = ident[4], ident[5]
                    endian = "<" if data_order == 1 else ">" if data_order == 2 else None
                    if endian and elf_class == 2:  # 64-bit ELF
                        f.seek(32)
                        phoff = struct.unpack(endian + "Q", f.read(8))[0]
                        f.seek(54)
                        phentsize, phnum = struct.unpack(endian + "HH", f.read(4))
                        ranges = []
                        for i in range(phnum):
                            f.seek(phoff + i * phentsize)
                            ph = f.read(phentsize)
                            if len(ph) < 40:
                                break
                            p_type = struct.unpack_from(endian + "I", ph, 0)[0]
                            p_flags = struct.unpack_from(endian + "I", ph, 4)[0]
                            p_offset = struct.unpack_from(endian + "Q", ph, 8)[0]
                            p_filesz = struct.unpack_from(endian + "Q", ph, 32)[0]
                            # PT_LOAD = 1, PF_X = 1
                            if p_type == 1 and (p_flags & 1):
                                s = max(0, p_offset)
                                e = min(file_size, p_offset + p_filesz)
                                if s < e:
                                    ranges.append((s, e))
                        if ranges:
                            ranges.sort()
                            merged = []
                            for s, e in ranges:
                                if merged and s <= merged[-1][1]:
                                    merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                                else:
                                    merged.append((s, e))
                            return tuple(merged)
    except Exception:
        return None
    return None

