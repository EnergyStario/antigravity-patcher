import os
import stat
import tempfile
import time

TEMP_SUFFIX = ".agtmp"


def fsync_dir(dirpath):
    """Синхронизировать директорию для надёжности переименования."""
    if os.name == "nt":
        return

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY

    try:
        fd = os.open(dirpath, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


def sweep_stale_temps(dirpath, max_age_seconds=600):
    """Очистить временные файлы *.agtmp старше max_age_seconds (по умолчанию 10 мин)."""
    if not dirpath or not os.path.isdir(dirpath):
        return 0
    now = time.time()
    removed = 0
    try:
        for entry in os.scandir(dirpath):
            try:
                if entry.name.endswith(TEMP_SUFFIX) and entry.is_file():
                    if now - entry.stat().st_mtime > max_age_seconds:
                        os.unlink(entry.path)
                        removed += 1
            except Exception:
                pass
    except Exception:
        pass
    return removed


def write_atomic(path, data, encoding="utf-8", kill_on_lock=True, meta_source=None):
    """
    Атомарная, защищённая от сбоев запись данных (bytes или str) по указанному пути.

    Гарантии:
    1. Исходный файл лежит нетронутым до завершения записи на 100%.
    2. Файл пишется во временный .agtmp рядом на том же томе диска.
    3. Подмена производится через атомарный системный вызов os.replace() (MFT-операция).
    4. При PermissionError на Windows точечно закрывает удерживающий процесс и повторяет замену.
    5. Автоматически убирает зависшие .agtmp старше 10 минут.
    """
    path = os.path.abspath(path)
    if os.path.islink(path):
        raise ValueError("Refusing to patch symlink")

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
        sweep_stale_temps(directory)

    if isinstance(data, str):
        payload = data.encode(encoding)
    elif isinstance(data, (bytes, bytearray, memoryview)):
        payload = bytes(data)
    else:
        raise TypeError(f"data must be bytes or str, got {type(data)}")

    tmp_path = f"{path}.{os.getpid()}{TEMP_SUFFIX}"

    source_for_meta = meta_source or (path if os.path.exists(path) else None)
    orig_stat = None
    if source_for_meta and os.path.exists(source_for_meta):
        try:
            orig_stat = os.stat(source_for_meta)
        except OSError:
            pass

    try:
        with open(tmp_path, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

            if orig_stat is not None and os.name == "posix":
                try:
                    os.fchown(f.fileno(), orig_stat.st_uid, orig_stat.st_gid)
                except OSError:
                    pass
                try:
                    os.fchmod(f.fileno(), stat.S_IMODE(orig_stat.st_mode))
                except OSError:
                    pass

        replaced = False
        for attempt in range(2):
            try:
                os.replace(tmp_path, path)
                replaced = True
                break
            except PermissionError as e:
                if attempt == 0 and kill_on_lock and os.name == "nt":
                    try:
                        from patcher.utils.admin import terminate_processes
                        fn = os.path.basename(path).lower()
                        procs = []
                        if "language_server" in fn:
                            procs.append("language_server.exe")
                        # Never kill user-facing IDE (Antigravity IDE.exe) or CLI (agy.exe)
                        if procs:
                            terminate_processes(procs)
                    except Exception:
                        pass
                    time.sleep(0.6)
                    continue
                raise e

        if not replaced:
            os.replace(tmp_path, path)

        if os.name == "posix" and directory:
            fsync_dir(directory)

        return True
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def atomic_replace_posix(path, data, meta_source=None):
    """
    Атомарная замена файла (обратная совместимость, делегирует в write_atomic).
    """
    return write_atomic(path, data, meta_source=meta_source, kill_on_lock=False)


def validate_patch_params(bdata, patches):
    """
    Валидация параметров патчей перед применением.

    Args:
        bdata: bytearray с данными файла
        patches: список патчей в формате (offset, gate_object)

    Raises:
        ValueError: если параметры патча некорректны
    """
    for off, g in patches:
        # Проверка на отрицательное смещение
        if off < 0:
            raise ValueError(f"Negative offset: {off}")

        fix = g.fix

        # Если есть оригинальные байты для проверки
        if hasattr(g, "orig"):
            orig = g.orig

            # Патч не должен менять размер файла
            if len(fix) != len(orig):
                raise ValueError(
                    f"Patch changes file size: orig={len(orig)}, fix={len(fix)}"
                )

            # Проверка выхода за границы файла
            if off + len(orig) > len(bdata):
                raise ValueError(
                    f"Patch offset out of bounds: offset={off}, "
                    f"patch_size={len(orig)}, file_size={len(bdata)}"
                )

            # Проверка соответствия оригинальных байтов
            if bdata[off:off + len(orig)] != orig:
                raise ValueError(
                    f"Original bytes mismatch at offset {off}"
                )
        else:
            # Без оригинала хотя бы проверяем границы
            if off + len(fix) > len(bdata):
                raise ValueError(
                    f"Patch offset out of bounds: offset={off}, "
                    f"patch_size={len(fix)}, file_size={len(bdata)}"
                )


def apply_patches_to_data(bdata, patches):
    """
    Применение патчей к bytearray с валидацией.

    Args:
        bdata: bytearray с данными файла
        patches: список патчей в формате (offset, gate_object)

    Returns:
        bytearray: модифицированные данные
    """
    # Валидация всех патчей перед применением
    validate_patch_params(bdata, patches)

    # Применяем патчи
    for off, g in patches:
        if hasattr(g, "orig"):
            bdata[off:off + len(g.orig)] = g.fix
        else:
            bdata[off:off + len(g.fix)] = g.fix

    return bdata
