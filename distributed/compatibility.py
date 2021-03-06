from __future__ import print_function, division, absolute_import

import sys

if sys.version_info[0] == 2:
    from Queue import Queue, Empty
    from io import BytesIO
    from thread import get_ident as get_thread_identity
    reload = reload
    unicode = unicode

    import gzip
    def gzip_decompress(b):
        f = GzipFile(fileobj=BytesIO(b))
        result = f.read()
        f.close()
        return result

    def isqueue(o):
        return (hasattr(o, 'queue') and
                hasattr(o, '__module__') and
                o.__module__ == 'Queue')


if sys.version_info[0] == 3:
    from queue import Queue, Empty
    from importlib import reload
    from threading import get_ident as get_thread_identity
    unicode = str
    from gzip import decompress as gzip_decompress

    def isqueue(o):
        return isinstance(o, Queue)


try:
    from functools import singledispatch
except ImportError:
    from singledispatch import singledispatch
