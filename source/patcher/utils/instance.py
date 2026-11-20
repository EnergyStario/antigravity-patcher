import os
import sys

_mutex_handle = None


def acquire_instance_lock(name: str = "Local\\AntigravityPatcher_SingleInstance") -> bool:
    """
    Acquire a cross-process single-instance lock to prevent concurrent write collisions.
    Returns True if this is the only running instance, False if another instance holds the lock.
    """
    global _mutex_handle
    if os.environ.get("AG_NO_INSTANCE_GUARD") == "1":
        return True

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        ERROR_ALREADY_EXISTS = 183

        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE

        handle = kernel32.CreateMutexW(None, True, name)
        last_error = kernel32.GetLastError()
        if last_error == ERROR_ALREADY_EXISTS:
            if handle:
                kernel32.CloseHandle(handle)
            return False
        _mutex_handle = handle
        return True
    else:
        try:
            import fcntl

            lock_dir = os.path.expanduser("~/.antigravity_tools")
            os.makedirs(lock_dir, exist_ok=True)
            lock_file = os.path.join(lock_dir, ".patcher.lock")
            f = open(lock_file, "a+")
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _mutex_handle = f
            return True
        except (IOError, OSError):
            return False


def release_instance_lock() -> None:
    """Release the single-instance lock if held."""
    global _mutex_handle
    if _mutex_handle is None:
        return

    if os.name == "nt":
        import ctypes

        try:
            ctypes.windll.kernel32.CloseHandle(_mutex_handle)
        except Exception:
            pass
        _mutex_handle = None
    else:
        try:
            _mutex_handle.close()
        except Exception:
            pass
        _mutex_handle = None

