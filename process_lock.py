"""OS-released lock: a crash cannot leave a permanently locked monitor."""
from contextlib import contextmanager


@contextmanager
def exclusive(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        try:
            import fcntl
        except ImportError:
            import msvcrt
            handle.seek(0)
            handle.write(b'0')
            handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError('Этот мониторинг уже запущен в другом окне') from None
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Этот мониторинг уже запущен в другом окне') from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
