from __future__ import division
from operator import add, sub

from collections import Iterator
from concurrent.futures import CancelledError
from datetime import timedelta
import itertools
from multiprocessing import Process
import os
import shutil
import sys
from threading import Thread
from time import sleep, time
import traceback

import pytest
from toolz import identity, isdistinct, first, concat, pluck
from tornado.ioloop import IOLoop
from tornado.iostream import IOStream
from tornado import gen

from dask.context import _globals
from dask.compatibility import apply
from distributed import Center, Worker, Nanny
from distributed.core import rpc, dumps, loads
from distributed.client import WrappedKey
from distributed.executor import (Executor, Future, CompatibleExecutor, _wait,
        wait, _as_completed, as_completed, tokenize, _global_executor,
        default_executor, _first_completed, ensure_default_get, futures_of)
from distributed.scheduler import Scheduler
from distributed.sizeof import sizeof
from distributed.utils import ignoring, sync, tmp_text
from distributed.utils_test import (cluster, cluster_center, slow,
        _test_cluster, _test_scheduler, loop, inc, dec, div, throws,
        gen_cluster, gen_test, double, deep)


@gen_cluster()
def test_submit(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(inc, 10)
    assert not x.done()

    assert isinstance(x, Future)
    assert x.executor is e
    result = yield x._result()
    assert result == 11
    assert x.done()

    y = e.submit(inc, 20)
    z = e.submit(add, x, y)
    result = yield z._result()
    assert result == 11 + 21
    yield e._shutdown()


@gen_cluster()
def test_map(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    L1 = e.map(inc, range(5))
    assert len(L1) == 5
    assert isdistinct(x.key for x in L1)
    assert all(isinstance(x, Future) for x in L1)

    result = yield L1[0]._result()
    assert result == inc(0)
    assert len(s.tasks) == 5

    L2 = e.map(inc, L1)

    result = yield L2[1]._result()
    assert result == inc(inc(1))
    assert len(s.tasks) == 10
    # assert L1[0].key in s.tasks[L2[0].key]

    total = e.submit(sum, L2)
    result = yield total._result()
    assert result == sum(map(inc, map(inc, range(5))))

    L3 = e.map(add, L1, L2)
    result = yield L3[1]._result()
    assert result == inc(1) + inc(inc(1))

    L4 = e.map(add, range(3), range(4))
    results = yield e._gather(L4)
    if sys.version_info[0] >= 3:
        assert results == list(map(add, range(3), range(4)))

    def f(x, y=10):
        return x + y

    L5 = e.map(f, range(5), y=5)
    results = yield e._gather(L5)
    assert results == list(range(5, 10))

    y = e.submit(f, 10)
    L6 = e.map(f, range(5), y=y)
    results = yield e._gather(L6)
    assert results == list(range(20, 25))

    yield e._shutdown()

@gen_cluster()
def test_compatible_map(s, a, b):
    e = CompatibleExecutor((s.ip, s.port), start=False)
    yield e._start()

    results = e.map(inc, range(5))
    assert not isinstance(results, list)
    # Since this map blocks as it waits for results,
    # waiting here will block the current IOLoop,
    # which happens to also be running the test Workers.
    # So wait on the results in a background thread to avoid blocking.
    f = gen.Future()
    def wait_on_results():
        f.set_result(list(results))
    t = Thread(target=wait_on_results)
    t.daemon = True
    t.start()
    result_list = yield f
    # getting map results blocks
    assert result_list == list(map(inc, range(5)))

    yield e._shutdown()


@gen_cluster()
def test_future(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(inc, 10)
    assert str(x.key) in repr(x)
    assert str(x.status) in repr(x)
    yield e._shutdown()


@gen_cluster()
def test_Future_exception(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(div, 1, 0)
    result = yield x._exception()
    assert isinstance(result, ZeroDivisionError)

    x = e.submit(div, 1, 1)
    result = yield x._exception()
    assert result is None
    yield e._shutdown()


def test_Future_exception_sync(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            x = e.submit(div, 1, 0)
            assert isinstance(x.exception(), ZeroDivisionError)

            x = e.submit(div, 1, 1)
            assert x.exception() is None


@gen_cluster()
def test_map_naming(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    L1 = e.map(inc, range(5))
    L2 = e.map(inc, range(5))

    assert [x.key for x in L1] == [x.key for x in L2]

    L3 = e.map(inc, [1, 1, 1, 1])
    assert len({x.event for x in L3}) == 1

    L4 = e.map(inc, [1, 1, 1, 1], pure=False)
    assert len({x.event for x in L4}) == 4

    yield e._shutdown()


@gen_cluster()
def test_submit_naming(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    a = e.submit(inc, 1)
    b = e.submit(inc, 1)

    assert a.event is b.event

    c = e.submit(inc, 1, pure=False)
    assert c.key != a.key

    yield e._shutdown()


@gen_cluster()
def test_exceptions(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(div, 1, 2)
    result = yield x._result()
    assert result == 1 / 2

    x = e.submit(div, 1, 0)
    with pytest.raises(ZeroDivisionError):
        result = yield x._result()

    x = e.submit(div, 10, 2)  # continues to operate
    result = yield x._result()
    assert result == 10 / 2

    yield e._shutdown()


@gen_cluster()
def test_gc(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(inc, 10)
    result = yield x._result()

    assert s.who_has[x.key]

    x.__del__()

    yield e._shutdown()

    assert not s.who_has[x.key]


def test_thread(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            x = e.submit(inc, 1)
            assert x.result() == 2


def test_sync_exceptions(loop):
    with cluster() as (s, [a, b]):
        e = Executor(('127.0.0.1', s['port']), loop=loop)

        x = e.submit(div, 10, 2)
        assert x.result() == 5

        y = e.submit(div, 10, 0)
        try:
            y.result()
            assert False
        except ZeroDivisionError:
            pass

        z = e.submit(div, 10, 5)
        assert z.result() == 2

        e.shutdown()


@gen_cluster()
def test_stress_1(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    n = 2**6

    seq = e.map(inc, range(n))
    while len(seq) > 1:
        yield gen.sleep(0.1)
        seq = [e.submit(add, seq[i], seq[i + 1])
                for i in range(0, len(seq), 2)]
    result = yield seq[0]._result()
    assert result == sum(map(inc, range(n)))

    yield e._shutdown()


@gen_cluster()
def test_gather(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(inc, 10)
    y = e.submit(inc, x)

    result = yield e._gather(x)
    assert result == 11
    result = yield e._gather([x])
    assert result == [11]
    result = yield e._gather({'x': x, 'y': [y]})
    assert result == {'x': 11, 'y': [12]}

    yield e._shutdown()


def test_gather_sync(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            x = e.submit(inc, 1)
            assert e.gather(x) == 2

            y = e.submit(div, 1, 0)

            with pytest.raises(ZeroDivisionError):
                e.gather([x, y])

            [xx] = e.gather([x, y], errors='skip')
            assert xx == 2


@gen_cluster()
def test_gather_strict(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(div, 2, 1)
    y = e.submit(div, 1, 0)

    with pytest.raises(ZeroDivisionError):
        result = yield e._gather([x, y])

    [xx] = yield e._gather([x, y], errors='skip')
    assert xx == 2

    yield e._shutdown()


@gen_cluster()
def test_get(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    result = yield e._get({'x': (inc, 1)}, 'x')
    assert result == 2

    result = yield e._get({'x': (inc, 1)}, ['x'])
    assert result == [2]

    result = yield e._get({}, [])
    assert result == []

    yield e._shutdown()


def test_get_sync(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            assert e.get({'x': (inc, 1)}, 'x') == 2



def test_submit_errors(loop):
    def f(a, b, c):
        pass

    e = Executor('127.0.0.1:8787', start=False, loop=loop)

    with pytest.raises(TypeError):
        e.submit(1, 2, 3)
    with pytest.raises(TypeError):
        e.map([1, 2, 3])


@gen_cluster()
def test_wait(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    a = e.submit(inc, 1)
    b = e.submit(inc, 1)
    c = e.submit(inc, 2)

    done, not_done = yield _wait([a, b, c])

    assert done == {a, b, c}
    assert not_done == set()
    assert a.status == b.status == 'finished'

    yield e._shutdown()


@gen_cluster()
def test__as_completed(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    a = e.submit(inc, 1)
    b = e.submit(inc, 1)
    c = e.submit(inc, 2)

    from distributed.compatibility import Queue
    queue = Queue()
    yield _as_completed([a, b, c], queue)

    assert queue.qsize() == 3
    assert {queue.get(), queue.get(), queue.get()} == {a, b, c}

    result = yield _first_completed([a, b, c])
    assert result in [a, b, c]

    yield e._shutdown()



def test_as_completed(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            x = e.submit(inc, 1)
            y = e.submit(inc, 2)
            z = e.submit(inc, 1)

            seq = as_completed([x, y, z])
            assert isinstance(seq, Iterator)
            assert set(seq) == {x, y, z}


def test_wait_sync(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            x = e.submit(inc, 1)
            y = e.submit(inc, 2)

            done, not_done = wait([x, y])
            assert done == {x, y}
            assert not_done == set()
            assert x.status == y.status == 'finished'


@gen_cluster()
def test_garbage_collection(s, a, b):
    import gc
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    a = e.submit(inc, 1)
    b = e.submit(inc, 1)

    assert e.refcount[a.key] == 2
    a.__del__()
    assert e.refcount[a.key] == 1

    c = e.submit(inc, b)
    b.__del__()

    result = yield c._result()
    assert result == 3

    bkey = b.key
    b.__del__()
    assert bkey not in e.futures
    yield e._shutdown()


@gen_cluster()
def test_garbage_collection_with_scatter(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    [a] = yield e._scatter([1])
    assert a.key in e.futures
    assert a.status == 'finished'
    assert a.event.is_set()
    assert s.who_wants[a.key] == {e.id}

    assert e.refcount[a.key] == 1
    a.__del__()
    assert e.refcount[a.key] == 0

    start = time()
    while True:
        if a.key not in s.who_has:
            break
        else:
            assert time() < start + 3
            yield gen.sleep(0.1)

    yield e._shutdown()


@gen_cluster(timeout=1000)
def test_recompute_released_key(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(inc, 100)
    result1 = yield x._result()
    xkey = x.key
    del x
    import gc; gc.collect()
    assert e.refcount[xkey] == 0

    # 1 second batching needs a second action to trigger
    while xkey in s.who_has or xkey in a.data or xkey in b.data:
        yield gen.sleep(0.1)

    x = e.submit(inc, 100)
    assert x.key in e.futures
    result2 = yield x._result()
    assert result1 == result2
    yield e._shutdown()


def slowinc(x):
    from time import sleep
    sleep(0.02)
    return x + 1


@pytest.mark.parametrize(('func', 'n'), [(slowinc, 100), (inc, 1000)])
def test_stress_gc(loop, func, n):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            x = e.submit(func, 1)
            for i in range(n):
                x = e.submit(func, x)

            assert x.result() == n + 2


@slow
def test_long_tasks_dont_trigger_timeout(loop):
    @gen.coroutine
    def f(c, a, b):
        e = Executor((c.ip, c.port), start=False,
                loop=loop)
        yield e._start()

        from time import sleep
        x = e.submit(sleep, 3)
        yield x._result()

        yield e._shutdown()
    _test_cluster(f, loop)


@gen_cluster()
def test_missing_data_heals(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(inc, 1)
    y = e.submit(inc, x)
    z = e.submit(inc, y)

    yield _wait([x, y, z])

    # Secretly delete y's key
    if y.key in a.data:
        del a.data[y.key]
    if y.key in b.data:
        del b.data[y.key]

    w = e.submit(add, y, z)

    result = yield w._result()
    assert result == 3 + 4
    yield e._shutdown()


@slow
@gen_cluster()
def test_missing_worker(s, a, b):
    bad = ('bad-host', 8788)
    s.ncores[bad] = 4
    s.who_has['b'] = {bad}
    s.has_what[bad] = {'b'}

    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    dsk = {'a': 1, 'b': (inc, 'a'), 'c': (inc, 'b')}

    result = yield e._get(dsk, 'c')
    assert result == 3
    assert bad not in s.ncores

    yield e._shutdown()


@gen_cluster()
def test_gather_robust_to_missing_data(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x, y, z = e.map(inc, range(3))
    yield _wait([x, y, z])  # everything computed

    for q in [x, y]:
        if q.key in a.data:
            del a.data[q.key]
        if q.key in b.data:
            del b.data[q.key]

    xx, yy, zz = yield e._gather([x, y, z])
    assert (xx, yy, zz) == (1, 2, 3)

    yield e._shutdown()


@gen_cluster()
def test_gather_robust_to_nested_missing_data(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    w = e.submit(inc, 1)
    x = e.submit(inc, w)
    y = e.submit(inc, x)
    z = e.submit(inc, y)

    yield _wait([z])

    for worker in [a, b]:
        for datum in [y, z]:
            if datum.key in worker.data:
                del worker.data[datum.key]

    result = yield e._gather([z])

    assert result == [inc(inc(inc(inc(1))))]

    yield e._shutdown()


@gen_cluster()
def test_tokenize_on_futures(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(inc, 1)
    y = e.submit(inc, 1)
    tok = tokenize(x)
    assert tokenize(x) == tokenize(x)
    assert tokenize(x) == tokenize(y)

    e.futures[x.key]['status'] = 'finished'

    assert tok == tokenize(y)

    yield e._shutdown()


@pytest.mark.skipif(sys.platform!='linux',
                    reason="Need 127.0.0.2 to mean localhost")
@gen_cluster([('127.0.0.1', 1), ('127.0.0.2', 2)])
def test_restrictions_submit(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(inc, 1, workers={a.ip})
    y = e.submit(inc, x, workers={b.ip})
    yield _wait([x, y])

    assert s.restrictions[x.key] == {a.ip}
    assert x.key in a.data

    assert s.restrictions[y.key] == {b.ip}
    assert y.key in b.data

    yield e._shutdown()


@pytest.mark.skipif(sys.platform!='linux',
                    reason="Need 127.0.0.2 to mean localhost")
@gen_cluster([('127.0.0.1', 1), ('127.0.0.2', 2)])
def test_restrictions_map(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    L = e.map(inc, range(5), workers={a.ip})
    yield _wait(L)

    assert set(a.data) == {x.key for x in L}
    assert not b.data
    for x in L:
        assert s.restrictions[x.key] == {a.ip}

    L = e.map(inc, [10, 11, 12], workers=[{a.ip},
                                          {a.ip, b.ip},
                                          {b.ip}])
    yield _wait(L)

    assert s.restrictions[L[0].key] == {a.ip}
    assert s.restrictions[L[1].key] == {a.ip, b.ip}
    assert s.restrictions[L[2].key] == {b.ip}

    with pytest.raises(ValueError):
        e.map(inc, [10, 11, 12], workers=[{a.ip}])

    yield e._shutdown()


@pytest.mark.skipif(sys.platform!='linux',
                    reason="Need 127.0.0.2 to mean localhost")
@gen_cluster([('127.0.0.1', 1), ('127.0.0.2', 2)])
def test_restrictions_get(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    dsk = {'x': 1, 'y': (inc, 'x'), 'z': (inc, 'y')}
    restrictions = {'y': {a.ip}, 'z': {b.ip}}

    result = yield e._get(dsk, ['y', 'z'], restrictions)
    assert result == [2, 3]
    assert 'y' in a.data
    assert 'z' in b.data

    yield e._shutdown()


@gen_cluster()
def dont_test_bad_restrictions_raise_exception(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    z = e.submit(inc, 2, workers={'bad-address'})
    try:
        yield z._result()
        assert False
    except ValueError as e:
        assert 'bad-address' in str(e)
        assert z.key in str(e)

    yield e._shutdown()


def test_submit_after_failed_worker(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            L = e.map(inc, range(10))
            wait(L)
            a['proc'].terminate()
            total = e.submit(sum, L)
            assert total.result() == sum(map(inc, range(10)))


def test_gather_after_failed_worker(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            L = e.map(inc, range(10))
            wait(L)
            a['proc'].terminate()
            result = e.gather(L)
            assert result == list(map(inc, range(10)))


@slow
def test_gather_then_submit_after_failed_workers(loop):
    with cluster(nworkers=4) as (s, [w, x, y, z]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            L = e.map(inc, range(20))
            wait(L)
            w['proc'].terminate()
            total = e.submit(sum, L)
            wait([total])

            (_, port) = first(e.scheduler.who_has[total.key])
            for d in [x, y, z]:
                if d['port'] == port:
                    d['proc'].terminate()

            result = e.gather([total])
            assert result == [sum(map(inc, range(20)))]


@gen_test()
def test_errors_dont_block():
    c = Center('127.0.0.1')
    c.listen(0)
    w = Worker(c.ip, c.port, ncores=1, ip='127.0.0.1')
    e = Executor((c.ip, c.port), start=False, loop=IOLoop.current())

    yield w._start()
    yield e._start()

    L = [e.submit(inc, 1),
         e.submit(throws, 1),
         e.submit(inc, 2),
         e.submit(throws, 2)]

    start = time()
    while not (L[0].status == L[2].status == 'finished'):
        assert time() < start + 5
        yield gen.sleep(0.01)

    result = yield e._gather([L[0], L[2]])
    assert result == [2, 3]

    yield w._close()
    c.stop()


@gen_cluster()
def test_submit_quotes(s, a, b):
    def assert_list(x, z=[]):
        return isinstance(x, list) and isinstance(z, list)

    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(assert_list, [1, 2, 3])
    result = yield x._result()
    assert result

    x = e.submit(assert_list, [1, 2, 3], z=[4, 5, 6])
    result = yield x._result()
    assert result

    x = e.submit(inc, 1)
    y = e.submit(inc, 2)
    z = e.submit(assert_list, [x, y])
    result = yield z._result()
    assert result

    yield e._shutdown()


@gen_cluster()
def test_map_quotes(s, a, b):
    def assert_list(x, z=[]):
        return isinstance(x, list) and isinstance(z, list)

    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    L = e.map(assert_list, [[1, 2, 3], [4]])
    result = yield e._gather(L)
    assert all(result)

    L = e.map(assert_list, [[1, 2, 3], [4]], z=[10])
    result = yield e._gather(L)
    assert all(result)

    L = e.map(assert_list, [[1, 2, 3], [4]], [[]] * 3)
    result = yield e._gather(L)
    assert all(result)

    yield e._shutdown()


@gen_cluster()
def test_two_consecutive_executors_share_results(s, a, b):
    from random import randint
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(randint, 0, 1000, pure=True)
    xx = yield x._result()

    f = Executor((s.ip, s.port), start=False)
    yield f._start()

    y = f.submit(randint, 0, 1000, pure=True)
    yy = yield y._result()

    assert xx == yy

    yield e._shutdown()
    yield f._shutdown()


@gen_cluster()
def test_submit_then_get_with_Future(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(slowinc, 1)
    dsk = {'y': (inc, x)}

    result = yield e._get(dsk, 'y')
    assert result == 3

    yield e._shutdown()


@gen_cluster()
def test_aliases(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(inc, 1)

    dsk = {'y': x}
    result = yield e._get(dsk, 'y')
    assert result == 2

    yield e._shutdown()


@gen_cluster()
def test__scatter(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    d = yield e._scatter({'y': 20})
    assert isinstance(d['y'], Future)
    assert a.data.get('y') == 20 or b.data.get('y') == 20
    assert (a.address in s.who_has['y'] or
            b.address in s.who_has['y'])
    assert s.who_has['y']
    assert s.nbytes == {'y': sizeof(20)}
    yy = yield e._gather([d['y']])
    assert yy == [20]

    [x] = yield e._scatter([10])
    assert isinstance(x, Future)
    assert a.data.get(x.key) == 10 or b.data.get(x.key) == 10
    xx = yield e._gather([x])
    assert s.who_has[x.key]
    assert (a.address in s.who_has[x.key] or
            b.address in s.who_has[x.key])
    assert s.nbytes == {'y': sizeof(20), x.key: sizeof(10)}
    assert xx == [10]

    z = e.submit(add, x, d['y'])  # submit works on Future
    result = yield z._result()
    assert result == 10 + 20
    result = yield e._gather([z, x])
    assert result == [30, 10]

    yield e._shutdown()


@gen_cluster()
def test_scatter_hash(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    [a] = yield e._scatter([1])
    [b] = yield e._scatter([1])

    assert a.key == b.key

    yield e._shutdown()


@gen_cluster()
def test_get_releases_data(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    [x] = yield e._get({'x': (inc, 1)}, ['x'])
    import gc; gc.collect()
    assert e.refcount['x'] == 0

    yield e._shutdown()


def test_global_executors(loop):
    assert not _global_executor[0]
    with pytest.raises(ValueError):
        default_executor()
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            assert _global_executor == [e]
            assert default_executor() is e
            with Executor(('127.0.0.1', s['port']), loop=loop) as f:
                assert _global_executor == [f]
                assert default_executor() is f
                assert default_executor(e) is e
                assert default_executor(f) is f

    assert not _global_executor[0]


@gen_cluster()
def test_exception_on_exception(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(lambda: 1 / 0)
    y = e.submit(inc, x)

    with pytest.raises(ZeroDivisionError):
        out = yield y._result()

    z = e.submit(inc, y)

    with pytest.raises(ZeroDivisionError):
        out = yield z._result()

    yield e._shutdown()


@gen_cluster()
def test_nbytes(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    [x] = yield e._scatter([1])
    assert s.nbytes == {x.key: sizeof(1)}

    y = e.submit(inc, x)
    yield y._result()

    assert s.nbytes == {x.key: sizeof(1),
                        y.key: sizeof(2)}

    yield e._shutdown()


@pytest.mark.skipif(sys.platform!='linux',
                    reason="Need 127.0.0.2 to mean localhost")
@gen_cluster([('127.0.0.1', 1), ('127.0.0.2', 2)])
def test_nbytes_determines_worker(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(identity, 1, workers=[a.address[0]])
    y = e.submit(identity, tuple(range(100)), workers=[b.address[0]])
    yield e._gather([x, y])

    z = e.submit(lambda x, y: None, x, y)
    yield z._result()
    assert s.who_has[z.key] == {b.address}

    yield e._shutdown()


@gen_cluster()
def test_pragmatic_move_small_data_to_large_data(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    lists = e.map(lambda n: list(range(n)), [10] * 10, pure=False)
    sums = e.map(sum, lists)
    total = e.submit(sum, sums)

    def f(x, y):
        return None
    results = e.map(f, lists, [total] * 10)

    yield _wait([total])

    yield _wait(results)

    for l, r in zip(lists, results):
        assert s.who_has[l.key] == s.who_has[r.key]

    yield e._shutdown()


@gen_cluster()
def test_get_with_non_list_key(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    dsk = {('x', 0): (inc, 1), 5: (inc, 2)}

    x = yield e._get(dsk, ('x', 0))
    y = yield e._get(dsk, 5)
    assert x == 2
    assert y == 3

    yield e._shutdown()


@gen_cluster()
def test_get_with_error(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    dsk = {'x': (div, 1, 0), 'y': (inc, 'x')}
    with pytest.raises(ZeroDivisionError):
        y = yield e._get(dsk, 'y')

    yield e._shutdown()


def test_get_with_error_sync(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            dsk = {'x': (div, 1, 0), 'y': (inc, 'x')}
            with pytest.raises(ZeroDivisionError):
                y = e.get(dsk, 'y')


@gen_cluster()
def test_directed_scatter(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    yield e._scatter([1, 2, 3], workers=[a.address])
    assert len(a.data) == 3
    assert not b.data

    yield e._shutdown()


def test_directed_scatter_sync(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            futures = e.scatter([1, 2, 3], workers=[('127.0.0.1', b['port'])])
            has_what = sync(loop, e.scheduler.has_what)
            assert len(has_what[('127.0.0.1', b['port'])]) == 3
            assert len(has_what[('127.0.0.1', a['port'])]) == 0


def test_iterator_scatter(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as ee:

            aa = ee.scatter([1,2,3])
            assert [1,2,3] == ee.gather(aa)

            g = (i for i in range(10))
            futures = ee.scatter(g)
            assert isinstance(futures, Iterator)

            a = next(futures)
            assert ee.gather(a) == 0


def test_queue_scatter(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as ee:
            from distributed.compatibility import Queue
            q = Queue()
            for d in range(10):
                q.put(d)

            futures = ee.scatter(q)
            assert isinstance(futures, Queue)
            a = futures.get()
            assert ee.gather(a) == 0


def test_queue_gather(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as ee:
            from distributed.compatibility import Queue
            q = Queue()

            qin = list(range(10))
            for d in qin:
                q.put(d)

            futures = ee.scatter(q)
            assert isinstance(futures, Queue)

            ff = ee.gather(futures)
            assert isinstance(ff, Queue)

            qout = []
            for f in range(10):
                qout.append(ff.get())
            assert qout == qin


def test_iterator_gather(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as ee:

            i_in = list(range(10))

            g = (d for d in i_in)
            futures = ee.scatter(g)
            assert isinstance(futures, Iterator)

            ff = ee.gather(futures)
            assert isinstance(ff, Iterator)

            i_out = list(ff)
            assert i_out == i_in

            i_in = ['a', 'b', 'c', StopIteration, 'd', 'e']

            g = (d for d in i_in)
            futures = ee.scatter(g)

            ff = ee.gather(futures)
            i_out = list(ff)
            assert i_out == i_in

@gen_cluster()
def test_many_submits_spread_evenly(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    L = [e.submit(inc, i) for i in range(10)]
    yield _wait(L)

    assert a.data and b.data

    yield e._shutdown()


@gen_cluster()
def test_traceback(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(div, 1, 0)
    tb = yield x._traceback()

    if sys.version_info[0] >= 3:
        assert any('x / y' in line
                   for line in pluck(3, traceback.extract_tb(tb)))

    yield e._shutdown()


def test_traceback_sync(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            x = e.submit(div, 1, 0)
            tb = x.traceback()
            if sys.version_info[0] >= 3:
                assert any('x / y' in line
                           for line in concat(traceback.extract_tb(tb))
                           if isinstance(line, str))

            y = e.submit(inc, x)
            tb2 = y.traceback()

            assert set(pluck(3, traceback.extract_tb(tb2))).issuperset(
                   set(pluck(3, traceback.extract_tb(tb))))

            z = e.submit(div, 1, 2)
            tb = z.traceback()
            assert tb is None


@gen_test()
def test_restart():
    from distributed import Nanny, rpc
    c = Center('127.0.0.1')
    c.listen(0)
    a = Nanny(c.ip, c.port, ncores=2, ip='127.0.0.1')
    b = Nanny(c.ip, c.port, ncores=2, ip='127.0.0.1')

    yield [a._start(), b._start()]

    e = Executor((c.ip, c.port), start=False, loop=IOLoop.current())
    yield e._start()

    assert e.scheduler.ncores == {a.worker_address: 2, b.worker_address: 2}

    x = e.submit(inc, 1)
    y = e.submit(inc, x)
    yield y._result()

    cc = rpc(ip=c.ip, port=c.port)
    who_has = yield cc.who_has()
    try:
        assert e.scheduler.who_has == who_has
        assert set(e.scheduler.who_has) == {x.key, y.key}

        f = yield e._restart()
        assert f is e

        assert len(e.scheduler.stacks) == 2
        assert len(e.scheduler.processing) == 2

        who_has = yield cc.who_has()
        assert not who_has
        assert not e.scheduler.who_has

        assert x.cancelled()
        assert y.cancelled()

    finally:
        yield a._close()
        yield b._close()
        yield e._shutdown(fast=True)
        c.stop()


def test_restart_sync_no_center(loop):
    with cluster(nanny=True) as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            x = e.submit(inc, 1)
            e.restart()
            assert x.cancelled()
            y = e.submit(inc, 2)
            assert y.result() == 3


def test_restart_sync(loop):
    with cluster_center(nanny=True) as (c, [a, b]):
        with Executor(('127.0.0.1', c['port']), loop=loop) as e:
            assert len(e.scheduler.has_what) == 2
            x = e.submit(div, 1, 2)
            x.result()

            assert e.scheduler.who_has
            e.restart()
            assert not e.scheduler.who_has
            assert x.cancelled()

            with pytest.raises(CancelledError):
                x.result()

            assert (set(e.scheduler.stacks) ==
                    set(e.scheduler.processing) ==
                    set(e.scheduler.ncores))
            assert len(e.scheduler.stacks) == 2

            y = e.submit(div, 1, 3)
            assert y.result() == 1 / 3


def test_restart_fast(loop):
    with cluster_center(nanny=True) as (c, [a, b]):
        with Executor(('127.0.0.1', c['port'])) as e:
            L = e.map(sleep, range(10))

            start = time()
            e.restart()
            assert not e.scheduler.tasks
            assert time() - start < 5

            assert all(x.status == 'cancelled' for x in L)

            x = e.submit(inc, 1)
            assert x.result() == 2


def test_fast_kill(loop):
    from distributed import Nanny, rpc
    c = Center('127.0.0.1')
    c.listen(0)
    a = Nanny(c.ip, c.port, ncores=2, ip='127.0.0.1')
    b = Nanny(c.ip, c.port, ncores=2, ip='127.0.0.1')
    e = Executor((c.ip, c.port), start=False, loop=loop)
    @gen.coroutine
    def f():
        yield a._start()
        yield b._start()

        while len(c.ncores) < 2:
            yield gen.sleep(0.01)
        yield e._start()

        L = e.map(sleep, range(10))

        try:
            start = time()
            yield e._restart()
            assert time() - start < 5

            assert all(x.status == 'cancelled' for x in L)

            x = e.submit(inc, 1)
            result = yield x._result()
            assert result == 2
        finally:
            yield a._close()
            yield b._close()
            yield e._shutdown(fast=True)
            c.stop()

    loop.run_sync(f)


@gen_cluster()
def test_upload_file(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    def g():
        import myfile
        return myfile.f()

    with tmp_text('myfile.py', 'def f():\n    return 123') as fn:
        yield e._upload_file(fn)

    sleep(1)  # TODO:  why is this necessary?
    x = e.submit(g, pure=False)
    result = yield x._result()
    assert result == 123

    with tmp_text('myfile.py', 'def f():\n    return 456') as fn:
        yield e._upload_file(fn)

    y = e.submit(g, pure=False)
    result = yield y._result()
    assert result == 456

    yield e._shutdown()


def test_upload_file_sync(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port'])) as e:
            def g():
                import myfile
                return myfile.x

            with tmp_text('myfile.py', 'x = 123') as fn:
                e.upload_file(fn)
                x = e.submit(g)
                assert x.result() == 123


@gen_cluster()
def test_upload_file_exception(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    with tmp_text('myfile.py', 'syntax-error!') as fn:
        with pytest.raises(SyntaxError):
            yield e._upload_file(fn)

    yield e._shutdown()


def test_upload_file_exception_sync(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port'])) as e:
            with tmp_text('myfile.py', 'syntax-error!') as fn:
                with pytest.raises(SyntaxError):
                    e.upload_file(fn)


@gen_cluster()
def test_multiple_executors(s, a, b):
        a = Executor((s.ip, s.port), start=False)
        yield a._start()
        b = Executor((s.ip, s.port), start=False)
        yield b._start()

        x = a.submit(inc, 1)
        y = b.submit(inc, 2)
        assert x.executor is a
        assert y.executor is b
        xx = yield x._result()
        yy = yield y._result()
        assert xx == 2
        assert yy == 3
        z = a.submit(add, x, y)
        assert z.executor is a
        zz = yield z._result()
        assert zz == 5

        yield a._shutdown()
        yield b._shutdown()


def test_multiple_executors_restart(loop):
    from distributed import Nanny, rpc
    c = Center('127.0.0.1')
    c.listen(0)
    a = Nanny(c.ip, c.port, ncores=2, ip='127.0.0.1')
    b = Nanny(c.ip, c.port, ncores=2, ip='127.0.0.1')
    @gen.coroutine
    def f():
        yield a._start()
        yield b._start()
        while len(c.ncores) < 2:
            yield gen.sleep(0.01)

        try:
            e1 = Executor((c.ip, c.port), start=False, loop=loop)
            yield e1._start()
            e2 = Executor(e1.scheduler, start=False, loop=loop)
            yield e2._start()

            x = e1.submit(inc, 1)
            y = e2.submit(inc, 2)
            xx = yield x._result()
            yy = yield y._result()
            assert xx == 2
            assert yy == 3

            yield e1._restart()

            assert x.cancelled()
            assert y.cancelled()
        finally:
            yield a._close()
            yield b._close()
            yield e1._shutdown(fast=True)
            yield e2._shutdown(fast=True)
            c.stop()

    loop.run_sync(f)


@gen_cluster()
def test_async_compute(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    from dask.imperative import do, value
    x = value(1)
    y = do(inc)(x)
    z = do(dec)(x)

    [yy, zz, aa] = e.compute([y, z, 3], sync=False)
    assert isinstance(yy, Future)
    assert isinstance(zz, Future)
    assert aa == 3

    result = yield e._gather([yy, zz])
    assert result == [2, 0]

    assert isinstance(e.compute(y), Future)
    assert isinstance(e.compute([y]), (tuple, list))

    yield e._shutdown()


def test_sync_compute(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port'])) as e:
            from dask.imperative import do, value
            x = value(1)
            y = do(inc)(x)
            z = do(dec)(x)

            yy, zz = e.compute([y, z], sync=True)
            assert (yy, zz) == (2, 0)


@gen_cluster()
def test_remote_scheduler(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    assert isinstance(e.scheduler_stream, IOStream)
    assert s.streams

    x = e.submit(inc, 1)
    result = yield x._result()

    yield e._shutdown()


def test_input_types(loop):
    @gen.coroutine
    def f(c, a, b):
        e1 = Executor((c.ip, c.port), start=False, loop=loop)
        yield e1._start()

        assert isinstance(e1.center, rpc)
        assert isinstance(e1.scheduler, Scheduler)

        s = Scheduler((c.ip, c.port))
        yield s.sync_center()
        done = s.start(0)

        e2 = Executor(s, start=False, loop=loop)
        yield e2._start()

        assert isinstance(e2.center, rpc)
        assert isinstance(e2.scheduler, Scheduler)

        s.listen(8042)

        e3 = Executor(('127.0.0.1', s.port), start=False, loop=loop)
        yield e3._start()

        assert isinstance(e3.center, rpc)
        assert isinstance(e3.scheduler, rpc)

        s.stop()

        yield e1._shutdown()
        yield e2._shutdown()
        yield e3._shutdown()
    _test_cluster(f, loop)


@gen_cluster()
def test_remote_scatter_gather(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x, y, z = yield e._scatter([1, 2, 3])

    assert x.key in a.data or x.key in b.data
    assert y.key in a.data or y.key in b.data
    assert z.key in a.data or z.key in b.data

    xx, yy, zz = yield e._gather([x, y, z])
    assert (xx, yy, zz) == (1, 2, 3)

    yield e._shutdown()


@gen_cluster(timeout=1000)
def test_remote_submit_on_Future(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(lambda x: x + 1, 1)
    y = e.submit(lambda x: x + 1, x)
    result = yield y._result()
    assert result == 3

    yield e._shutdown()


def test_start_is_idempotent(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            e.start()
            e.start()
            e.start()

            x = e.submit(inc, 1)
            assert x.result() == 2


def test_executor_with_scheduler(loop):
    @gen.coroutine
    def f(s, a, b):
        assert s.ncores == {a.address: a.ncores, b.address: b.ncores}
        e = Executor(('127.0.0.1', s.port), start=False, loop=loop)
        yield e._start()

        x = e.submit(inc, 1)
        y = e.submit(inc, 2)
        z = e.submit(add, x, y)
        result = yield x._result()
        assert result == 1 + 1

        a, b, c = yield e._scatter([1, 2, 3])
        aa, bb, xx = yield e._gather([a, b, x])
        assert (aa, bb, xx) == (1, 2, 2)

        result = yield e._get({'x': (inc, 1), 'y': (add, 'x', 10)}, 'y')
        assert result == 12

        yield e._shutdown()

    _test_scheduler(f)


@pytest.mark.skipif(sys.platform!='linux',
                    reason="Need 127.0.0.2 to mean localhost")
@gen_cluster([('127.0.0.1', 1), ('127.0.0.2', 2)])
def test_allow_restrictions(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(inc, 1, workers=a.ip)
    yield x._result()
    assert s.who_has[x.key] == {a.address}
    assert not s.loose_restrictions

    x = e.submit(inc, 2, workers=a.ip, allow_other_workers=True)
    yield x._result()
    assert s.who_has[x.key] == {a.address}
    assert x.key in s.loose_restrictions

    L = e.map(inc, range(3, 13), workers=a.ip, allow_other_workers=True)
    yield _wait(L)
    assert all(s.who_has[f.key] == {a.address} for f in L)
    assert {f.key for f in L}.issubset(s.loose_restrictions)

    """
    x = e.submit(inc, 14, workers='127.0.0.3')
    with ignoring(gen.TimeoutError):
        yield gen.with_timeout(timedelta(seconds=0.1), x._result())
        assert False
    assert not s.who_has[x.key]
    assert x.key not in s.loose_restrictions
    """

    x = e.submit(inc, 15, workers='127.0.0.3', allow_other_workers=True)
    yield x._result()
    assert s.who_has[x.key]
    assert x.key in s.loose_restrictions

    L = e.map(inc, range(15, 25), workers='127.0.0.3', allow_other_workers=True)
    yield _wait(L)
    assert all(s.who_has[f.key] for f in L)
    assert {f.key for f in L}.issubset(s.loose_restrictions)

    with pytest.raises(ValueError):
        e.submit(inc, 1, allow_other_workers=True)

    with pytest.raises(ValueError):
        e.map(inc, [1], allow_other_workers=True)

    with pytest.raises(TypeError):
        e.submit(inc, 20, workers='127.0.0.1', allow_other_workers='Hello!')

    with pytest.raises(TypeError):
        e.map(inc, [20], workers='127.0.0.1', allow_other_workers='Hello!')

    yield e._shutdown()


@pytest.mark.skipif('True', reason='because')
def test_bad_address():
    try:
        Executor('123.123.123.123:1234', timeout=0.1)
    except (IOError, gen.TimeoutError) as e:
        assert "connect" in str(e).lower()

    try:
        Executor('127.0.0.1:1234', timeout=0.1)
    except (IOError, gen.TimeoutError) as e:
        assert "connect" in str(e).lower()


@gen_cluster()
def test_long_error(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    def bad(x):
        raise ValueError('a' * 100000)

    x = e.submit(bad, 10)

    try:
        yield x._result()
    except ValueError as e:
        assert len(str(e)) < 100000

    tb = yield x._traceback()
    assert all(len(line) < 100000
               for line in concat(traceback.extract_tb(tb))
               if isinstance(line, str))


@gen_cluster()
def test_map_on_futures_with_kwargs(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    def f(x, y=10):
        return x + y

    futures = e.map(inc, range(10))
    futures2 = e.map(f, futures, y=20)
    results = yield e._gather(futures2)
    assert results == [i + 1 + 20 for i in range(10)]

    future = e.submit(inc, 100)
    future2 = e.submit(f, future, y=200)
    result = yield future2._result()
    assert result == 100 + 1 + 200
    yield e._shutdown()


@gen_cluster(Worker=Nanny, timeout=60)
def test_failed_worker_without_warning(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    L = e.map(inc, range(10))
    yield _wait(L)

    ncores1 = s.ncores.copy()

    a.process.terminate()
    start = time()
    while not a.process.is_alive():
        yield gen.sleep(0.01)
        assert time() - start < 10

    yield gen.sleep(0.5)

    start = time()
    while len(s.ncores) < 2:
        yield gen.sleep(0.01)
        assert time() - start < 10

    yield _wait(L)

    L2 = e.map(inc, range(10, 20))
    yield _wait(L2)
    assert all(len(keys) > 0 for keys in s.has_what.values())
    ncores2 = s.ncores.copy()

    yield e._restart()

    L = e.map(inc, range(10))
    yield _wait(L)
    assert all(len(keys) > 0 for keys in s.has_what.values())

    assert not (set(ncores2) & set(s.ncores))  # no overlap


class BadlySerializedObject(object):
    def __getstate__(self):
        return 1
    def __setstate__(self, state):
        raise TypeError("hello!")


class FatallySerializedObject(object):
    def __getstate__(self):
        return 1
    def __setstate__(self, state):
        print("This should never have been deserialized, closing")
        import sys
        sys.exit(0)


@gen_cluster()
def test_badly_serialized_input(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    o = BadlySerializedObject()

    future = e.submit(inc, o)
    futures = e.map(inc, range(10))

    L = yield e._gather(futures)
    assert list(L) == list(map(inc, range(10)))

    yield e._shutdown()


@pytest.mark.xfail
def test_badly_serialized_input_stderr(capsys):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port'])) as e:
            o = BadlySerializedObject()
            future = e.submit(inc, o)

            start = time()
            while True:
                sleep(0.01)
                out, err = capsys.readouterr()
                if 'hello!' in err:
                    break
                assert time() - start < 20


@gen_cluster()
def test_repr(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    assert s.ip in str(e)
    assert str(s.port) in repr(e)

    yield e._shutdown()


@gen_cluster()
def test_forget_simple(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(inc, 1)
    y = e.submit(inc, 2)
    z = e.submit(add, x, y, workers=[a.ip], allow_other_workers=True)

    yield _wait([x, y, z])
    assert not s.waiting_data[x.key]
    assert not s.waiting_data[y.key]

    assert set(s.tasks) == {x.key, y.key, z.key}

    s.client_releases_keys(keys=[x.key], client=e.id)
    assert x.key in s.tasks
    s.client_releases_keys(keys=[z.key], client=e.id)
    for coll in [s.tasks, s.dependencies, s.dependents, s.waiting,
            s.waiting_data, s.who_has, s.restrictions, s.loose_restrictions,
            s.in_play, s.keyorder, s.exceptions, s.who_wants,
            s.exceptions_blame]:
        assert x.key not in coll
        assert z.key not in coll

    assert z.key not in s.dependents[y.key]

    s.client_releases_keys(keys=[y.key], client=e.id)
    assert not s.tasks

    yield e._shutdown()


@gen_cluster()
def test_forget_complex(s, A, B):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    a, b, c, d = yield e._scatter(list(range(4)))
    ab = e.submit(add, a, b)
    cd = e.submit(add, c, d)
    ac = e.submit(add, a, c)
    acab = e.submit(add, ac, ab)

    yield _wait([a,b,c,d,ab,ac,cd,acab])

    assert set(s.tasks) == {f.key for f in [ab,ac,cd,acab]}

    s.client_releases_keys(keys=[ab.key], client=e.id)
    assert set(s.tasks) == {f.key for f in [ab,ac,cd,acab]}

    s.client_releases_keys(keys=[b.key], client=e.id)
    assert set(s.tasks) == {f.key for f in [ab,ac,cd,acab]}

    s.client_releases_keys(keys=[acab.key], client=e.id)
    assert set(s.tasks) == {f.key for f in [ac,cd]}
    assert b.key not in s.who_has

    start = time()
    while b.key in A.data or b.key in B.data:
        yield gen.sleep(0.01)
        assert time() < start + 10

    s.client_releases_keys(keys=[ac.key], client=e.id)
    assert set(s.tasks) == {f.key for f in [cd]}

    yield e._shutdown()


def test_repr_sync(loop):
    with cluster(nworkers=3) as (s, [a, b, c]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            s = str(e)
            r = repr(e)
            assert e.scheduler.ip in s
            assert str(e.scheduler.port) in r
            assert str(3) in s  # nworkers
            assert 'threads' in s


@gen_cluster()
def test_waiting_data(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(inc, 1)
    y = e.submit(inc, 2)
    z = e.submit(add, x, y, workers=[a.ip], allow_other_workers=True)

    yield _wait([x, y, z])

    assert x.key not in s.waiting_data[x.key]
    assert y.key not in s.waiting_data[y.key]
    assert not s.waiting_data[x.key]
    assert not s.waiting_data[y.key]

    yield e._shutdown()


@gen_cluster()
def test_multi_executor(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    f = Executor((s.ip, s.port), start=False)
    yield f._start()

    assert set(s.streams) == {e.id, f.id}

    x = e.submit(inc, 1)
    y = f.submit(inc, 2)
    y2 = e.submit(inc, 2)

    assert y.key == y2.key

    yield _wait([x, y])

    assert s.wants_what == {e.id: {x.key, y.key}, f.id: {y.key}}
    assert s.who_wants == {x.key: {e.id}, y.key: {e.id, f.id}}

    yield e._shutdown()

    start = time()
    while e.id in s.wants_what:
        yield gen.sleep(0.01)
        assert time() < start + 5

    assert e.id not in s.wants_what
    assert e.id not in s.who_wants[y.key]
    assert x.key not in s.who_wants

    yield f._shutdown()

    assert not s.tasks


@gen_cluster()
def test_cleanup_after_broken_executor_connection(s, a, b):
    def f(ip, port):
        e = Executor((ip, port))
        x = e.submit(lambda x: x + 1, 10)
        x.result()
        sleep(100)

    proc = Process(target=f, args=(s.ip, s.port))
    proc.daemon = True
    proc.start()

    start = time()
    while not s.tasks:
        yield gen.sleep(0.01)
        assert time() < start + 5

    proc.terminate()

    start = time()
    while s.tasks:
        yield gen.sleep(0.01)
        assert time() < start + 5



@gen_cluster()
def test_multi_garbage_collection(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    f = Executor((s.ip, s.port), start=False)
    yield f._start()

    x = e.submit(inc, 1)
    y = f.submit(inc, 2)
    y2 = e.submit(inc, 2)

    assert y.key == y2.key

    yield _wait([x, y])

    x.__del__()
    start = time()
    while x.key in a.data or x.key in b.data:
        yield gen.sleep(0.01)
        assert time() < start + 5

    assert s.wants_what == {e.id: {y.key}, f.id: {y.key}}
    assert s.who_wants == {y.key: {e.id, f.id}}

    y.__del__()
    start = time()
    while x.key in s.wants_what[f.id]:
        yield gen.sleep(0.01)
        assert time() < start + 5

    yield gen.sleep(0.1)
    assert y.key in a.data or y.key in b.data
    assert s.wants_what == {e.id: {y.key}, f.id: set()}
    assert s.who_wants == {y.key: {e.id}}

    y2.__del__()
    start = time()
    while y.key in a.data or y.key in b.data:
        yield gen.sleep(0.01)
        assert time() < start + 5

    assert not any(v for v in s.wants_what.values())
    assert not s.who_wants

    yield e._shutdown()
    yield f._shutdown()


@gen_cluster()
def test__broadcast(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x, y = yield e._scatter([1, 2], broadcast=True)

    assert a.data == b.data == {x.key: 1, y.key: 2}

    yield e._shutdown()


def test_broadcast(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            x, y = e.scatter([1, 2], broadcast=True)

            has_what = sync(e.loop, e.scheduler.has_what)

            assert has_what == {('127.0.0.1', a['port']): {x.key, y.key},
                                ('127.0.0.1', b['port']): {x.key, y.key}}

            [z] = e.scatter([3], broadcast=True,
                            workers=[('127.0.0.1', a['port'])])

            has_what = sync(e.loop, e.scheduler.has_what)
            assert has_what == {('127.0.0.1', a['port']): {x.key, y.key, z.key},
                                ('127.0.0.1', b['port']): {x.key, y.key}}


@gen_cluster()
def test__cancel(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(slowinc, 1)
    y = e.submit(slowinc, x)

    while y.key not in s.tasks:
        yield gen.sleep(0.01)

    yield e._cancel([x], block=True)

    assert x.cancelled()
    assert 'cancel' in str(x)
    s.validate()

    start = time()
    while not y.cancelled():
        yield gen.sleep(0.01)
        assert time() < start + 5

    assert not s.tasks
    s.validate()

    yield e._shutdown()


@gen_cluster()
def test__cancel_multi_client(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()
    f = Executor((s.ip, s.port), start=False)
    yield f._start()

    x = e.submit(slowinc, 1)
    y = f.submit(slowinc, 1)

    assert x.key == y.key

    yield e._cancel([x], block=True)

    assert x.cancelled()
    assert not y.cancelled()

    assert y.key in s.tasks

    out = yield y._result()
    assert out == 2

    with pytest.raises(CancelledError):
        yield x._result()

    yield e._shutdown()
    yield f._shutdown()


@gen_cluster()
def test__cancel_collection(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    import dask.bag as db

    L = e.map(double, [[1], [2], [3]])
    x = db.Bag({('b', i): f for i, f in enumerate(L)}, 'b', 3)

    yield e._cancel(x)
    yield e._cancel([x])
    assert all(f.cancelled() for f in L)
    assert not s.tasks

    yield e._shutdown()


def test_cancel(loop):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            x = e.submit(slowinc, 1)
            y = e.submit(slowinc, x)
            z = e.submit(slowinc, y)

            e.cancel([y], block=True)

            start = time()
            while not z.cancelled():
                sleep(0.01)
                assert time() < start + 5

            assert x.result() == 2

            z.cancel(block=True)
            assert z.cancelled()


@gen_cluster()
def test_future_type(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(inc, 1)
    yield _wait([x])
    assert x.type == int
    assert 'int' in str(x)

    yield e._shutdown()


@gen_cluster()
def test_traceback_clean(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(div, 1, 0)
    try:
        yield x._result()
    except Exception as e:
        f = e
        exc_type, exc_value, tb = sys.exc_info()
        while tb:
            assert 'scheduler' not in tb.tb_frame.f_code.co_filename
            assert 'worker' not in tb.tb_frame.f_code.co_filename
            tb = tb.tb_next


@gen_cluster()
def test_map_queue(s, a, b):
    from distributed.compatibility import Queue, isqueue
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    q_1 = Queue(maxsize=2)
    q_2 = e.map(inc, q_1)
    assert isqueue(q_2)
    q_3 = e.map(double, q_2)
    assert isqueue(q_3)
    q_4 = yield e._gather(q_3)
    assert isqueue(q_4)

    q_1.put(1)

    f = q_4.get()
    assert isinstance(f, Future)
    result = yield f._result()
    assert result == (1 + 1) * 2

    yield e._shutdown()


@gen_cluster()
def test_map_iterator(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = iter([1, 2, 3])
    y = iter([10, 20, 30])
    f1 = e.map(add, x, y)
    assert isinstance(f1, Iterator)

    start = time()  # ensure that we compute eagerly
    while not s.tasks:
        yield gen.sleep(0.01)
        assert time() < start + 5

    f2 = e.map(double, f1)
    assert isinstance(f2, Iterator)

    future = next(f2)
    result = yield future._result()
    assert result == (1 + 10) * 2

    yield e._shutdown()


@gen_cluster()
def test_map_infinite_iterators(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    futures = e.map(add, [1, 2], itertools.repeat(10))
    assert len(futures) == 2


@gen_cluster()
def test_map_differnet_lengths(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    assert len(e.map(add, [1, 2], [1, 2, 3])) == 2


def test_Future_exception_sync(loop, capsys):
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            ensure_default_get(e)
            ensure_default_get(e)
            ensure_default_get(e)
            ensure_default_get(e)

    out, err = capsys.readouterr()
    assert len(out.strip().split('\n')) == 1

    assert _globals['get'] == e.get


@gen_cluster(timeout=1000)
def test_async_persist(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    from dask.imperative import do, value, Value
    x = value(1)
    y = do(inc)(x)
    z = do(dec)(x)
    w = do(add)(y, z)

    yy, ww = e.persist([y, w])
    assert type(yy) == type(y)
    assert type(ww) == type(w)
    assert len(yy.dask) == 1
    assert len(ww.dask) == 1
    assert len(w.dask) > 1
    assert y._keys() == yy._keys()
    assert w._keys() == ww._keys()

    while y.key not in s.tasks and w.key not in s.tasks:
        yield gen.sleep(0.01)

    assert s.who_wants[y.key] == {e.id}
    assert s.who_wants[w.key] == {e.id}

    yyf, wwf = e.compute([yy, ww])
    yyy, www = yield e._gather([yyf, wwf])
    assert yyy == inc(1)
    assert www == add(inc(1), dec(1))

    assert isinstance(e.persist(y), Value)
    assert isinstance(e.persist([y]), (list, tuple))

    yield e._shutdown()


def test_persist(loop):
    pytest.importorskip('dask.array')
    import dask.array as da
    with cluster() as (s, [a, b]):
        with Executor(('127.0.0.1', s['port']), loop=loop) as e:
            x = da.ones((10, 10), chunks=(5, 10))
            y = 2 * (x + 1)
            assert len(y.dask) == 6
            yy = e.persist(y)
            assert len(y.dask) == 6
            assert len(yy.dask) == 2
            assert all(isinstance(v, Future) for v in yy.dask.values())

            assert (yy.compute(get=e.get) == y.compute(get=e.get)).all()


@gen_cluster(timeout=60)
def test_long_traceback(s, a, b):
    from distributed.core import dumps
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    n = sys.getrecursionlimit()
    sys.setrecursionlimit(500)

    try:
        x = e.submit(deep, 1000)
        yield _wait([x])
        assert len(dumps(e.futures[x.key]['traceback'])) < 10000
        assert isinstance(e.futures[x.key]['exception'], RuntimeError)
    finally:
        sys.setrecursionlimit(n)

    yield e._shutdown()


@gen_cluster()
def test_wait_on_collections(s, a, b):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    import dask.bag as db

    L = e.map(double, [[1], [2], [3]])
    x = db.Bag({('b', i): f for i, f in enumerate(L)}, 'b', 3)

    yield _wait(x)
    assert all(f.key in a.data or f.key in b.data for f in L)

    yield e._shutdown()


def test_futures_of():
    x, y, z = map(WrappedKey, 'xyz')

    assert futures_of(0) == []
    assert futures_of(x) == [x]
    assert futures_of([x, y, z]) == [x, y, z]
    assert futures_of([x, [y], [[z]]]) == [x, y, z]

    import dask.bag as db
    b = db.Bag({('b', i): f for i, f in enumerate([x, y, z])}, 'b', 3)
    assert set(futures_of(b)) == {x, y, z}


@gen_cluster(ncores=[('127.0.0.1', 1)])
def test_dont_delete_recomputed_results(s, w):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    x = e.submit(inc, 1)                        # compute first time
    yield _wait([x])
    x.__del__()                                 # trigger garbage collection
    xx = e.submit(inc, 1)                       # compute second time

    start = time()
    while xx.key not in w.data:                               # data shows up
        yield gen.sleep(0.01)
        assert time() < start + 1

    while time() < start + (s.delete_interval + 100) / 1000:  # and stays
        assert xx.key in w.data
        yield gen.sleep(0.01)

    yield e._shutdown()


@gen_cluster(ncores=[])
def test_fatally_serialized_input(s):
    e = Executor((s.ip, s.port), start=False)
    yield e._start()

    o = FatallySerializedObject()

    future = e.submit(inc, o)

    while not s.tasks:
        yield gen.sleep(0.01)

    yield e._shutdown()
