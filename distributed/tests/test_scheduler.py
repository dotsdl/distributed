from collections import defaultdict
from copy import deepcopy
from operator import add
from time import time

from dask.core import get_deps
from toolz import merge, concat, valmap
from tornado.queues import Queue
from tornado.iostream import StreamClosedError
from tornado.gen import TimeoutError
import pytest

from distributed import Center, Nanny, Worker
from distributed.core import connect, read, write, rpc, loads
from distributed.client import WrappedKey
from distributed.scheduler import (validate_state, heal, update_state,
        decide_worker, assign_many_tasks, heal_missing_data, Scheduler,
        _maybe_complex, dumps_function, dumps_task, apply)
from distributed.utils_test import inc, ignoring, dec


alice = 'alice'
bob = 'bob'

def test_heal():
    dsk = {'x': 1, 'y': (inc, 'x')}
    dependencies = {'x': set(), 'y': {'x'}}
    dependents = {'x': {'y'}, 'y': set()}

    who_has = dict()
    stacks = {'alice': [], 'bob': []}
    processing = {'alice': set(), 'bob': set()}

    waiting = {'x': set(), 'y': {'x'}}
    waiting_data = {'x': {'y'}, 'y': set()}
    finished_results = set()

    local = {k: v for k, v in locals().items() if '@' not in k}

    output = heal(dependencies, dependents, who_has, stacks, processing,
            {}, {})

    assert output['dependencies'] == dependencies
    assert output['dependents'] == dependents
    assert output['who_has'] == who_has
    assert output['processing'] == processing
    assert output['stacks'] == stacks
    assert output['waiting'] == waiting
    assert output['waiting_data'] == waiting_data
    assert output['released'] == set()

    state = {'who_has': dict(),
             'stacks': {'alice': ['x'], 'bob': []},
             'processing': {'alice': set(), 'bob': set()},
             'waiting': {}, 'waiting_data': {}}

    heal(dependencies, dependents, **state)


def test_heal_2():
    dsk = {'x': 1, 'y': (inc, 'x'), 'z': (inc, 'y'),
           'a': 1, 'b': (inc, 'a'), 'c': (inc, 'b'),
           'result': (add, 'z', 'c')}
    dependencies = {'x': set(), 'y': {'x'}, 'z': {'y'},
                    'a': set(), 'b': {'a'}, 'c': {'b'},
                    'result': {'z', 'c'}}
    dependents = {'x': {'y'}, 'y': {'z'}, 'z': {'result'},
                  'a': {'b'}, 'b': {'c'}, 'c': {'result'},
                  'result': set()}

    state = {'who_has': {'y': {alice}, 'a': {alice}},  # missing 'b'
             'stacks': {'alice': ['z'], 'bob': []},
             'processing': {'alice': set(), 'bob': set(['c'])},
             'waiting': {}, 'waiting_data': {}}

    output = heal(dependencies, dependents, **state)
    assert output['waiting'] == {'b': set(), 'c': {'b'}, 'result': {'c', 'z'}}
    assert output['waiting_data'] == {'a': {'b'}, 'b': {'c'}, 'c': {'result'},
                                      'y': {'z'}, 'z': {'result'},
                                      'result': set()}
    assert output['who_has'] == {'y': {alice}, 'a': {alice}}
    assert output['stacks'] == {'alice': ['z'], 'bob': []}
    assert output['processing'] == {'alice': set(), 'bob': set()}
    assert output['released'] == {'x'}


def test_heal_restarts_leaf_tasks():
    dsk = {'a': 1, 'b': (inc, 'a'), 'c': (inc, 'b'),
           'x': 1, 'y': (inc, 'x'), 'z': (inc, 'y')}
    dependents, dependencies = get_deps(dsk)

    state = {'who_has': dict(),  # missing 'b'
             'stacks': {'alice': ['a'], 'bob': ['x']},
             'processing': {'alice': set(), 'bob': set()},
             'waiting': {}, 'waiting_data': {}}

    del state['stacks']['bob']
    del state['processing']['bob']

    output = heal(dependencies, dependents, **state)
    assert 'x' in output['waiting']


def test_heal_culls():
    dsk = {'a': 1, 'b': (inc, 'a'), 'c': (inc, 'b'),
           'x': 1, 'y': (inc, 'x'), 'z': (inc, 'y')}
    dependencies, dependents = get_deps(dsk)

    state = {'who_has': {'c': {alice}, 'y': {alice}},
             'stacks': {'alice': ['a'], 'bob': []},
             'processing': {'alice': set(), 'bob': set('y')},
             'waiting': {}, 'waiting_data': {}}

    output = heal(dependencies, dependents, **state)
    assert 'a' not in output['stacks']['alice']
    assert output['released'] == {'a', 'b', 'x'}
    assert output['finished_results'] == {'c'}
    assert 'y' not in output['processing']['bob']

    assert output['waiting']['z'] == set()


def test_update_state():
    dsk = {'x': 1, 'y': (inc, 'x')}
    dependencies = {'x': set(), 'y': {'x'}}
    dependents = {'x': {'y'}, 'y': set()}

    waiting = {'y': set()}
    waiting_data = {'x': {'y'}, 'y': set()}

    who_wants = defaultdict(set, y={'client'})
    wants_what = defaultdict(set, client={'y'})
    who_has = {'x': {'alice'}}
    processing = set()
    released = set()
    in_play = {'x', 'y'}

    new_dsk = {'a': 1, 'z': (add, 'y', 'a')}
    new_dependencies = {'z': {'y', 'a'}}
    new_keys = {'z'}

    e_dsk = {'x': 1, 'y': (inc, 'x'), 'a': 1, 'z': (add, 'y', 'a')}
    e_dependencies = {'x': set(), 'a': set(), 'y': {'x'}, 'z': {'a', 'y'}}
    e_dependents = {'z': set(), 'y': {'z'}, 'a': {'z'}, 'x': {'y'}}

    e_waiting = {'y': set(), 'a': set(), 'z': {'a', 'y'}}
    e_waiting_data = {'x': {'y'}, 'y': {'z'}, 'a': {'z'}, 'z': set()}

    e_who_wants = {'z': {'client'}, 'y': {'client'}}
    e_wants_what = {'client': {'y', 'z'}}

    update_state(dsk, dependencies, dependents, who_wants, wants_what,
                 who_has, in_play,
                 waiting, waiting_data, new_dsk, new_keys, new_dependencies,
                 'client')

    assert dsk == e_dsk
    assert dependencies == e_dependencies
    assert dependents == e_dependents
    assert waiting == e_waiting
    assert waiting_data == e_waiting_data
    assert who_wants == e_who_wants
    assert wants_what == e_wants_what
    assert in_play == {'x', 'y', 'a', 'z'}


def test_update_state_with_processing():
    dsk = {'x': 1, 'y': (inc, 'x'), 'z': (inc, 'y')}
    dependencies, dependents = get_deps(dsk)

    waiting = {'z': {'y'}}
    waiting_data = {'x': {'y'}, 'y': {'z'}, 'z': set()}

    who_wants = defaultdict(set, z={'client'})
    wants_what = defaultdict(set, client={'z'})
    who_has = {'x': {'alice'}}
    processing = {'y'}
    released = set()
    in_play = {'z', 'x', 'y'}

    new_dsk = {'a': (inc, 'x'), 'b': (add, 'a', 'y'), 'c': (inc, 'z')}
    new_dependencies = {'a': {'x'}, 'b': {'a', 'y'}, 'c': {'z'}}
    new_keys = {'b', 'c'}

    e_waiting = {'z': {'y'}, 'a': set(), 'b': {'a', 'y'}, 'c': {'z'}}
    e_waiting_data = {'x': {'y', 'a'}, 'y': {'z', 'b'}, 'z': {'c'},
                      'a': {'b'}, 'b': set(), 'c': set()}

    e_who_wants = {'b': {'client'}, 'c': {'client'}, 'z': {'client'}}
    e_wants_what = {'client': {'b', 'c', 'z'}}

    update_state(dsk, dependencies, dependents, who_wants, wants_what,
                 who_has, in_play,
                 waiting, waiting_data, new_dsk, new_keys, new_dependencies,
                 'client')

    assert waiting == e_waiting
    assert waiting_data == e_waiting_data
    assert who_wants == e_who_wants
    assert wants_what == e_wants_what
    assert in_play == {'x', 'y', 'z', 'a', 'b', 'c'}


def test_update_state_respects_data_in_memory():
    dsk = {'x': 1, 'y': (inc, 'x')}
    dependencies, dependents = get_deps(dsk)

    waiting = dict()
    waiting_data = {'y': set()}

    who_wants = defaultdict(set, y={'client'})
    wants_what = defaultdict(set, client={'y'})
    who_has = {'y': {'alice'}}
    processing = set()
    released = {'x'}
    in_play = {'y'}

    new_dsk = {'x': 1, 'y': (inc, 'x'), 'z': (add, 'y', 'x')}
    new_dependencies = {'y': {'x'}, 'z': {'y', 'x'}}
    new_keys = {'z'}

    e_dsk = new_dsk.copy()
    e_waiting = {'z': {'x'}, 'x': set()}
    e_waiting_data = {'x': {'z'}, 'y': {'z'}, 'z': set()}
    e_who_wants = {'y': {'client'}, 'z': {'client'}}
    e_wants_what = {'client': {'y', 'z'}}

    update_state(dsk, dependencies, dependents, who_wants, wants_what,
                 who_has, in_play,
                 waiting, waiting_data, new_dsk, new_keys, new_dependencies,
                 'client')

    assert dsk == e_dsk
    assert waiting == e_waiting
    assert waiting_data == e_waiting_data
    assert who_wants == e_who_wants
    assert wants_what == e_wants_what
    assert in_play == {'x', 'y', 'z'}


def test_update_state_supports_recomputing_released_results():
    dsk = {'x': 1, 'y': (inc, 'x'), 'z': (inc, 'x')}
    dependencies, dependents = get_deps(dsk)

    waiting = dict()
    waiting_data = {'z': set()}

    who_wants = defaultdict(set, z={'client'})
    wants_what = defaultdict(set, client={'z'})
    who_has = {'z': {'alice'}}
    processing = set()
    released = {'x', 'y'}
    in_play = {'z'}

    new_dsk = {'x': 1, 'y': (inc, 'x')}
    new_dependencies = {'y': {'x'}}
    new_keys = {'y'}

    e_dsk = dsk.copy()
    e_waiting = {'x': set(), 'y': {'x'}}
    e_waiting_data = {'x': {'y'}, 'y': set(), 'z': set()}
    e_who_wants = {'z': {'client'}, 'y': {'client'}}
    e_wants_what = {'client': {'y', 'z'}}

    update_state(dsk, dependencies, dependents, who_wants, wants_what,
                 who_has, in_play,
                 waiting, waiting_data, new_dsk, new_keys, new_dependencies,
                 'client')

    assert dsk == e_dsk
    assert waiting == e_waiting
    assert waiting_data == e_waiting_data
    assert who_wants == e_who_wants
    assert wants_what == e_wants_what
    assert in_play == {'x', 'y', 'z'}


def test_decide_worker_with_many_independent_leaves():
    dsk = merge({('y', i): (inc, ('x', i)) for i in range(100)},
                {('x', i): i for i in range(100)})
    dependencies, dependents = get_deps(dsk)
    stacks = {'alice': [], 'bob': []}
    who_has = merge({('x', i * 2): {'alice'} for i in range(50)},
                    {('x', i * 2 + 1): {'bob'} for i in range(50)})
    nbytes = {k: 0 for k in who_has}

    for key in dsk:
        worker = decide_worker(dependencies, stacks, who_has, {}, set(), nbytes, key)
        stacks[worker].append(key)

    nhits = (len([k for k in stacks['alice'] if 'alice' in who_has[('x', k[1])]])
             + len([k for k in stacks['bob'] if 'bob' in who_has[('x', k[1])]]))

    assert nhits > 90


def test_decide_worker_with_restrictions():
    dependencies = {'x': set()}
    alice, bob, charlie = ('alice', 8000), ('bob', 8000), ('charlie', 8000)
    stacks = {alice: [], bob: [], charlie: []}
    who_has = {}
    restrictions = {'x': {'alice', 'charlie'}}
    nbytes = {}
    result = decide_worker(dependencies, stacks, who_has, restrictions, set(), nbytes, 'x')
    assert result in {alice, charlie}


    stacks = {alice: [1, 2, 3], bob: [], charlie: [4, 5, 6]}
    result = decide_worker(dependencies, stacks, who_has, restrictions, set(), nbytes, 'x')
    assert result in {alice, charlie}

    dependencies = {'x': {'y'}}
    who_has = {'y': {bob}}
    nbytes = {'y': 0}
    result = decide_worker(dependencies, stacks, who_has, restrictions, set(), nbytes, 'x')
    assert result in {alice, charlie}


def test_decide_worker_with_loose_restrictions():
    dependencies = {'x': set()}
    alice, bob, charlie = ('alice', 8000), ('bob', 8000), ('charlie', 8000)
    stacks = {alice: [1, 2, 3], bob: [], charlie: [1]}
    who_has = {}
    nbytes = {}
    restrictions = {'x': {'alice', 'charlie'}}

    result = decide_worker(dependencies, stacks, who_has, restrictions,
                           set(), nbytes, 'x')
    assert result == charlie

    result = decide_worker(dependencies, stacks, who_has, restrictions,
                           {'x'}, nbytes, 'x')
    assert result == charlie

    restrictions = {'x': {'david', 'ethel'}}
    with pytest.raises(ValueError):
        result = decide_worker(dependencies, stacks, who_has, restrictions,
                               set(), nbytes, 'x')

    restrictions = {'x': {'david', 'ethel'}}
    result = decide_worker(dependencies, stacks, who_has, restrictions,
                           {'x'}, nbytes, 'x')
    assert result == bob



def test_decide_worker_without_stacks():
    with pytest.raises(ValueError):
        result = decide_worker({'x': []}, [], {}, {}, set(), {}, 'x')


def test_validate_state():
    dsk = {'x': 1, 'y': (inc, 'x')}
    dependencies = {'x': set(), 'y': {'x'}}
    waiting = {'y': {'x'}, 'x': set()}
    dependents = {'x': {'y'}, 'y': set()}
    waiting_data = {'x': {'y'}}
    who_has = dict()
    stacks = {'alice': [], 'bob': []}
    processing = {'alice': set(), 'bob': set()}
    finished_results = set()
    released = set()
    in_play = {'x', 'y'}
    who_wants = {'y': {'client'}}
    wants_what = {'client': {'y'}}

    validate_state(**locals())

    who_has['x'] = {alice}
    with pytest.raises(Exception):
        validate_state(**locals())

    del waiting['x']
    with pytest.raises(Exception):
        validate_state(**locals())

    waiting['y'].remove('x')
    validate_state(**locals())

    stacks['alice'].append('y')
    with pytest.raises(Exception):
        validate_state(**locals())

    waiting.pop('y')
    validate_state(**locals())

    stacks['alice'].pop()
    with pytest.raises(Exception):
        validate_state(**locals())

    processing['alice'].add('y')
    validate_state(**locals())

    processing['alice'].pop()
    with pytest.raises(Exception):
        validate_state(**locals())

    who_has['y'] = {alice}
    with pytest.raises(Exception):
        validate_state(**locals())

    finished_results.add('y')
    validate_state(**locals())


def test_assign_many_tasks():
    alice, bob, charlie = ('alice', 8000), ('bob', 8000), ('charlie', 8000)
    dependencies = {'y': {'x'}, 'b': {'a'}, 'x': set(), 'a': set()}
    waiting = {'y': set(), 'b': {'a'}, 'a': set()}
    keyorder = {c: 1 for c in 'abxy'}
    who_has = {'x': {alice}}
    stacks = {alice: [], bob: []}
    nbytes = {}
    restrictions = {}
    keys = ['y', 'a']

    new_stacks = assign_many_tasks(dependencies, waiting, keyorder, who_has,
                                   stacks, restrictions, set(), nbytes, ['y', 'a'])

    assert 'y' in stacks[alice]
    assert 'a' in stacks[alice] + stacks[bob]
    assert 'a' not in waiting
    assert 'y' not in waiting

    assert set(concat(new_stacks.values())) == set(concat(stacks.values()))


def test_assign_many_tasks_with_restrictions():
    alice, bob, charlie = ('alice', 8000), ('bob', 8000), ('charlie', 8000)
    dependencies = {'y': {'x'}, 'b': {'a'}, 'x': set(), 'a': set()}
    waiting = {'y': set(), 'b': {'a'}, 'a': set()}
    keyorder = {c: 1 for c in 'abxy'}
    who_has = {'x': {alice}}
    stacks = {alice: [], bob: []}
    nbytes = {'x': 0}
    restrictions = {'y': {'bob'}, 'a': {'alice'}}
    keys = ['y', 'a']

    new_stacks = assign_many_tasks(dependencies, waiting, keyorder, who_has,
                                   stacks, restrictions, set(), nbytes, ['y', 'a'])

    assert 'y' in stacks[bob]
    assert 'a' in stacks[alice]
    assert 'a' not in waiting
    assert 'y' not in waiting

    assert set(concat(new_stacks.values())) == set(concat(stacks.values()))


def test_fill_missing_data():
    dsk = {'x': 1, 'y': (inc, 'x'), 'z': (inc, 'y')}
    dependencies, dependents = get_deps(dsk)

    waiting = {}
    waiting_data = {'z': set()}

    who_wants = defaultdict(set, z={'client'})
    wants_what = defaultdict(set, client={'z'})
    who_has = {'z': {alice}}
    processing = set()
    released = set()
    in_play = {'z'}

    e_waiting = {'x': set(), 'y': {'x'}, 'z': {'y'}}
    e_waiting_data = {'x': {'y'}, 'y': {'z'}, 'z': set()}
    e_in_play = {'x', 'y', 'z'}

    lost = {'z'}
    del who_has['z']
    in_play.remove('z')

    heal_missing_data(dsk, dependencies, dependents, who_has, in_play, waiting,
            waiting_data, lost)

    assert waiting == e_waiting
    assert waiting_data == e_waiting_data
    assert in_play == e_in_play


from distributed.utils_test import gen_cluster, gen_test
from distributed.utils import All
from tornado import gen

def div(x, y):
    return x / y

@gen_cluster()
def test_scheduler(s, a, b):
    stream = yield connect(s.ip, s.port)
    yield write(stream, {'op': 'register-client', 'client': 'ident'})
    msg = yield read(stream)
    assert msg['op'] == 'stream-start'

    # Test update graph
    yield write(stream, {'op': 'update-graph',
                         'tasks': {'x': (inc, 1),
                                   'y': (inc, 'x'),
                                   'z': (inc, 'y')},
                         'dependencies': {'x': set(),
                                          'y': {'x'},
                                          'z': {'y'}},
                         'keys': ['x', 'z'],
                         'client': 'ident'})
    while True:
        msg = yield read(stream)
        if msg['op'] == 'key-in-memory' and msg['key'] == 'z':
            break

    assert a.data.get('x') == 2 or b.data.get('x') == 2

    # Test erring tasks
    yield write(stream, {'op': 'update-graph',
                         'tasks': {'a': (div, 1, 0),
                                 'b': (inc, 'a')},
                         'dependencies': {'a': set(),
                                          'b': {'a'}},
                         'keys': ['a', 'b'],
                         'client': 'ident'})

    while True:
        msg = yield read(stream)
        if msg['op'] == 'task-erred' and msg['key'] == 'b':
            break

    # Test missing data
    yield write(stream, {'op': 'missing-data', 'missing': ['z']})

    while True:
        msg = yield read(stream)
        if msg['op'] == 'key-in-memory' and msg['key'] == 'z':
            break

    # Test missing data without being informed
    for w in [a, b]:
        if 'z' in w.data:
            del w.data['z']
    yield write(stream, {'op': 'update-graph',
                         'tasks': {'zz': (inc, 'z')},
                         'dependencies': {'zz': {'z'}},
                         'keys': ['zz'],
                         'client': 'ident'})
    while True:
        msg = yield read(stream)
        if msg['op'] == 'key-in-memory' and msg['key'] == 'zz':
            break

    write(stream, {'op': 'close'})
    stream.close()


@gen_cluster()
def test_multi_queues(s, a, b):
    sched, report = Queue(), Queue()
    s.handle_queues(sched, report)

    msg = yield report.get()
    assert msg['op'] == 'stream-start'

    # Test update graph
    sched.put_nowait({'op': 'update-graph',
                      'tasks': {'x': (inc, 1),
                              'y': (inc, 'x'),
                              'z': (inc, 'y')},
                      'dependencies': {'x': set(),
                                       'y': {'x'},
                                       'z': {'y'}},
                      'keys': ['z']})

    while True:
        msg = yield report.get()
        if msg['op'] == 'key-in-memory' and msg['key'] == 'z':
            break

    slen, rlen = len(s.scheduler_queues), len(s.report_queues)
    sched2, report2 = Queue(), Queue()
    s.handle_queues(sched2, report2)
    assert slen + 1 == len(s.scheduler_queues)
    assert rlen + 1 == len(s.report_queues)

    sched2.put_nowait({'op': 'update-graph',
                       'tasks': {'a': (inc, 10)},
                       'dependencies': {'a': set()},
                       'keys': ['a']})

    for q in [report, report2]:
        while True:
            msg = yield q.get()
            if msg['op'] == 'key-in-memory' and msg['key'] == 'a':
                break


@gen_cluster()
def test_server(s, a, b):
    stream = yield connect('127.0.0.1', s.port)
    yield write(stream, {'op': 'register-client', 'client': 'ident'})
    yield write(stream, {'op': 'update-graph',
                         'tasks': {'x': (inc, 1), 'y': (inc, 'x')},
                         'dependencies': {'x': set(), 'y': {'x'}},
                         'keys': ['y'],
                         'client': 'ident'})

    while True:
        msg = yield read(stream)
        if msg['op'] == 'key-in-memory' and msg['key'] == 'y':
            break

    yield write(stream, {'op': 'close-stream'})
    msg = yield read(stream)
    assert msg == {'op': 'stream-closed'}
    assert stream.closed()
    stream.close()


@gen_cluster()
def test_server_listens_to_other_ops(s, a, b):
    r = rpc(ip='127.0.0.1', port=s.port)
    ident = yield r.identity()
    assert ident['type'] == 'Scheduler'


@gen_cluster()
def test_remove_worker_from_scheduler(s, a, b):
    dsk = {('x', i): (inc, i) for i in range(10)}
    s.update_graph(tasks=valmap(dumps_task, dsk), keys=list(dsk),
                   dependencies={k: set() for k in dsk})
    assert s.stacks[a.address]

    assert a.address in s.worker_queues
    s.remove_worker(address=a.address)
    assert a.address not in s.ncores
    assert len(s.stacks[b.address]) + len(s.processing[b.address]) == \
            len(dsk)  # b owns everything
    assert all(k in s.in_play for k in dsk)
    s.validate()


@gen_cluster()
def test_add_worker(s, a, b):
    w = Worker(s.ip, s.port, ncores=3, ip='127.0.0.1')
    w.data[('x', 5)] = 6
    w.data['y'] = 1
    yield w._start(0)

    dsk = {('x', i): (inc, i) for i in range(10)}
    s.update_graph(tasks=valmap(dumps_task, dsk), keys=list(dsk), client='client',
                   dependencies={k: set() for k in dsk})

    s.add_worker(address=w.address, keys=list(w.data),
                 ncores=w.ncores, services=s.services)

    for k in w.data:
        assert w.address in s.who_has[k]

    s.validate(allow_overlap=True)


@gen_cluster()
def test_feed(s, a, b):
    def func(scheduler):
        return scheduler.processing, scheduler.stacks

    stream = yield connect(s.ip, s.port)
    yield write(stream, {'op': 'feed',
                         'function': func,
                         'interval': 0.01})

    for i in range(5):
        response = yield read(stream)
        expected = s.processing, s.stacks

    stream.close()


@gen_cluster()
def test_feed_setup_teardown(s, a, b):
    def setup(scheduler):
        return 1

    def func(scheduler, state):
        assert state == 1
        return b'OK'

    def teardown(scheduler, state):
        scheduler.flag = 'done'

    stream = yield connect(s.ip, s.port)
    yield write(stream, {'op': 'feed',
                         'function': func,
                         'setup': setup,
                         'teardown': teardown,
                         'interval': 0.01})

    for i in range(5):
        response = yield read(stream)
        assert response == b'OK'

    stream.close()
    start = time()
    while not hasattr(s, 'flag'):
        yield gen.sleep(0.01)
        assert time() - start < 5


@gen_test()
def test_scheduler_as_center():
    s = Scheduler()
    done = s.start(0)
    a = Worker('127.0.0.1', s.port, ip='127.0.0.1', ncores=1)
    a.data.update({'x': 1, 'y': 2})
    b = Worker('127.0.0.1', s.port, ip='127.0.0.1', ncores=2)
    b.data.update({'y': 2, 'z': 3})
    c = Worker('127.0.0.1', s.port, ip='127.0.0.1', ncores=3)
    yield [w._start() for w in [a, b, c]]

    assert s.ncores == {w.address: w.ncores for w in [a, b, c]}
    assert s.who_has == {'x': {a.address},
                         'y': {a.address, b.address},
                         'z': {b.address}}

    s.update_graph(tasks={'a': dumps_task((inc, 1))},
                   keys=['a'],
                   dependencies={'a': set()})
    while not s.who_has['a']:
        yield gen.sleep(0.01)
    assert 'a' in a.data or 'a' in b.data or 'a' in c.data

    yield [w._close() for w in [a, b, c]]

    assert s.ncores == {}
    assert s.who_has == {}

    yield s.close()


@gen_cluster()
def test_delete_data(s, a, b):
    yield s.scatter(data={'x': 1, 'y': 2, 'z': 3})
    assert set(a.data) | set(b.data) == {'x', 'y', 'z'}

    s.delete_data(keys=['x', 'y'])
    yield s.clear_data_from_workers()
    assert set(a.data) | set(b.data) == {'z'}


@gen_cluster()
def test_rpc(s, a, b):
    aa = s.rpc(ip=a.ip, port=a.port)
    aa2 = s.rpc(ip=a.ip, port=a.port)
    bb = s.rpc(ip=b.ip, port=b.port)
    assert aa is aa2
    assert aa is not bb


@gen_cluster()
def test_delete_callback(s, a, b):
    a.data['x'] = 1
    s.who_has['x'].add(a.address)
    s.has_what[a.address].add('x')

    s.delete_data(keys=['x'])
    assert not s.who_has['x']
    assert not s.has_what['y']
    assert a.data['x'] == 1  # still in memory
    assert s.deleted_keys == {a.address: {'x'}}
    yield s.clear_data_from_workers()
    assert not s.deleted_keys
    assert not a.data

    assert s._delete_periodic_callback.is_running()


@gen_cluster()
def test_self_aliases(s, a, b):
    a.data['a'] = 1
    s.update_data(who_has={'a': {a.address}}, nbytes={'a': 10}, client='client')
    s.update_graph(tasks=valmap(dumps_task, {'a': 'a', 'b': (inc, 'a')}),
                   keys=['b'], client='client',
                   dependencies={'b': {'a'}})

    sched, report = Queue(), Queue()
    s.handle_queues(sched, report)
    msg = yield report.get()
    assert msg['op'] == 'stream-start'

    while True:
        msg = yield report.get()
        if msg['op'] == 'key-in-memory' and msg['key'] == 'b':
            break


@gen_cluster()
def test_filtered_communication(s, a, b):
    e = yield connect(ip=s.ip, port=s.port)
    f = yield connect(ip=s.ip, port=s.port)
    yield write(e, {'op': 'register-client', 'client': 'e'})
    yield write(f, {'op': 'register-client', 'client': 'f'})
    yield read(e)
    yield read(f)

    assert set(s.streams) == {'e', 'f'}

    yield write(e, {'op': 'update-graph',
                    'tasks': {'x': (inc, 1), 'y': (inc, 'x')},
                    'dependencies': {'x': set(), 'y': {'x'}},
                    'client': 'e',
                    'keys': ['y']})

    yield write(f, {'op': 'update-graph',
                    'tasks': {'x': (inc, 1), 'z': (add, 'x', 10)},
                    'dependencies': {'x': set(), 'z': {'x'}},
                    'client': 'f',
                    'keys': ['z']})

    msg = yield read(e)
    assert msg['op'] == 'key-in-memory'
    assert msg['key'] == 'y'
    msg = yield read(f)
    assert msg['op'] == 'key-in-memory'
    assert msg['key'] == 'z'


def test_maybe_complex():
    assert not _maybe_complex(1)
    assert not _maybe_complex('x')
    assert _maybe_complex((inc, 1))
    assert _maybe_complex([(inc, 1)])
    assert _maybe_complex([(inc, 1)])
    assert _maybe_complex({'x': (inc, 1)})


def test_dumps_function():
    a = dumps_function(inc)
    assert loads(a)(10) == 11

    b = dumps_function(inc)
    assert a is b

    c = dumps_function(dec)
    assert a != c


def test_dumps_task():
    d = dumps_task((inc, 1))
    assert set(d) == {'function', 'args'}

    f = lambda x, y=2: x + y
    d = dumps_task((apply, f, (1,), {'y': 10}))
    assert loads(d['function'])(1, 2) == 3
    assert loads(d['args']) == (1,)
    assert loads(d['kwargs']) == {'y': 10}
