import copy
import pickle
from array import array

import msgpack
import pytest
from tlz import identity

try:
    import numpy as np
except ImportError:
    np = None

from dask.utils_test import inc

from distributed import wait
from distributed.comm.utils import from_frames, to_frames
from distributed.protocol import (
    Serialize,
    Serialized,
    dask_serialize,
    deserialize,
    deserialize_bytes,
    dumps,
    loads,
    nested_deserialize,
    register_serialization,
    register_serialization_family,
    serialize,
    serialize_bytelist,
    serialize_bytes,
    to_serialize,
)
from distributed.protocol.serialize import check_dask_serializable
from distributed.utils import nbytes
from distributed.utils_test import gen_test, inc


class MyObj:
    def __init__(self, data):
        self.data = data

    def __getstate__(self):
        raise Exception("Not picklable")


def serialize_myobj(x):
    return {}, [pickle.dumps(x.data)]


def deserialize_myobj(header, frames):
    return MyObj(pickle.loads(frames[0]))


register_serialization(MyObj, serialize_myobj, deserialize_myobj)


def test_dumps_serialize():
    for x in [123, [1, 2, 3, 4, 5, 6]]:
        header, frames = serialize(x)
        assert header["serializer"] == "pickle"
        assert len(frames) == 1

        result = deserialize(header, frames)
        assert result == x

    x = MyObj(123)
    header, frames = serialize(x)
    assert header["type"]
    assert len(frames) == 1

    result = deserialize(header, frames)
    assert result.data == x.data


def test_serialize_bytestrings():
    for b in (b"123", bytearray(b"4567")):
        header, frames = serialize(b)
        assert frames[0] is b
        bb = deserialize(header, frames)
        assert type(bb) == type(b)
        assert bb == b
        bb = deserialize(header, list(map(memoryview, frames)))
        assert type(bb) == type(b)
        assert bb == b
        bb = deserialize(header, [b"", *frames])
        assert type(bb) == type(b)
        assert bb == b


@pytest.mark.parametrize(
    "typecode", ["b", "B", "h", "H", "i", "I", "l", "L", "q", "Q", "f", "d"]
)
def test_serialize_arrays(typecode):
    a = array(typecode)
    a.extend(range(5))
    header, frames = serialize(a)
    assert frames[0] == memoryview(a)
    a2 = deserialize(header, frames)
    assert type(a2) == type(a)
    assert a2.typecode == a.typecode
    assert a2 == a


def test_Serialize():
    s = Serialize(123)
    assert "123" in str(s)
    assert s.data == 123

    t = Serialize((1, 2))
    assert str(t)

    u = Serialize(123)
    assert s == u
    assert not (s != u)
    assert s != t
    assert not (s == t)
    assert hash(s) == hash(u)
    assert hash(s) != hash(t)  # most probably


def test_Serialized():
    s = Serialized(*serialize(123))
    t = Serialized(*serialize((1, 2)))
    u = Serialized(*serialize(123))
    assert s == u
    assert not (s != u)
    assert s != t
    assert not (s == t)


def test_nested_deserialize():
    x = {
        "op": "update",
        "x": [to_serialize(123), to_serialize(456), 789],
        "y": {"a": ["abc", Serialized(*serialize("def"))], "b": b"ghi"},
    }
    x_orig = copy.deepcopy(x)

    assert nested_deserialize(x) == {
        "op": "update",
        "x": [123, 456, 789],
        "y": {"a": ["abc", "def"], "b": b"ghi"},
    }
    assert x == x_orig  # x wasn't mutated


from dask import delayed

from distributed.utils_test import gen_cluster


@gen_cluster(client=True)
async def test_object_in_graph(c, s, a, b):
    o = MyObj(123)
    v = delayed(o)
    v2 = delayed(identity)(v)

    future = c.compute(v2)
    result = await future

    assert isinstance(result, MyObj)
    assert result.data == 123


@gen_cluster(client=True)
async def test_scatter(c, s, a, b):
    o = MyObj(123)
    [future] = await c._scatter([o])
    await c._replicate(o)
    o2 = await c._gather(future)
    assert isinstance(o2, MyObj)
    assert o2.data == 123


@gen_cluster(client=True)
async def test_inter_worker_comms(c, s, a, b):
    o = MyObj(123)
    [future] = await c._scatter([o], workers=a.address)
    future2 = c.submit(identity, future, workers=b.address)
    o2 = await c._gather(future2)
    assert isinstance(o2, MyObj)
    assert o2.data == 123


class Empty:
    def __getstate__(self):
        raise Exception("Not picklable")


def serialize_empty(x):
    return {}, []


def deserialize_empty(header, frames):
    return Empty()


register_serialization(Empty, serialize_empty, deserialize_empty)


def test_empty():
    e = Empty()
    e2 = deserialize(*serialize(e))
    assert isinstance(e2, Empty)


def test_empty_loads():
    e = Empty()
    e2 = loads(dumps([to_serialize(e)]))
    assert isinstance(e2[0], Empty)


def test_empty_loads_deep():
    e = Empty()
    e2 = loads(dumps([[[to_serialize(e)]]]))
    assert isinstance(e2[0][0][0], Empty)


@pytest.mark.skipif(np is None, reason="Test needs numpy")
@pytest.mark.parametrize("kwargs", [{}, {"serializers": ["pickle"]}])
def test_serialize_bytes(kwargs):
    for x in [
        1,
        "abc",
        np.arange(5),
        b"ab" * int(40e6),
        int(2 ** 26) * b"ab",
        (int(2 ** 25) * b"ab", int(2 ** 25) * b"ab"),
    ]:
        b = serialize_bytes(x, **kwargs)
        assert isinstance(b, bytes)
        y = deserialize_bytes(b)
        assert str(x) == str(y)


@pytest.mark.skipif(np is None, reason="Test needs numpy")
def test_serialize_list_compress():
    pytest.importorskip("lz4")
    x = np.ones(1000000)
    L = serialize_bytelist(x)
    assert sum(map(nbytes, L)) < x.nbytes / 2

    b = b"".join(L)
    y = deserialize_bytes(b)
    assert (x == y).all()


def test_malicious_exception():
    class BadException(Exception):
        def __setstate__(self):
            return Exception("Sneaky deserialization code")

    class MyClass:
        def __getstate__(self):
            raise BadException()

    obj = MyClass()

    header, frames = serialize(obj, serializers=[])
    with pytest.raises(Exception) as info:
        deserialize(header, frames)

    assert "Sneaky" not in str(info.value)
    assert "MyClass" in str(info.value)

    header, frames = serialize(obj, serializers=["pickle"])
    with pytest.raises(Exception) as info:
        deserialize(header, frames)

    assert "Sneaky" not in str(info.value)
    assert "BadException" in str(info.value)


def test_errors():
    msg = {"data": {"foo": to_serialize(inc)}, "a": 1, "b": 2, "c": 3, "d": 4, "e": 5}

    header, frames = serialize(msg, serializers=["msgpack", "pickle"])
    assert header["serializer"] == "pickle"

    header, frames = serialize(msg, serializers=["msgpack"])
    assert header["serializer"] == "error"

    with pytest.raises(TypeError):
        serialize(msg, serializers=["msgpack"], on_error="raise")


@gen_test()
async def test_err_on_bad_deserializer():
    frames = await to_frames({"x": to_serialize(1234)}, serializers=["pickle"])

    result = await from_frames(frames, deserializers=["pickle", "foo"])
    assert result == {"x": 1234}

    with pytest.raises(TypeError):
        await from_frames(frames, deserializers=["msgpack"])


class MyObject:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def my_dumps(obj, context=None):
    if type(obj).__name__ == "MyObject":
        header = {"serializer": "my-ser"}
        frames = [
            msgpack.dumps(obj.__dict__, use_bin_type=True),
            msgpack.dumps(context, use_bin_type=True),
        ]
        return header, frames
    else:
        raise NotImplementedError()


def my_loads(header, frames):
    obj = MyObject(**msgpack.loads(frames[0], raw=False))

    # to provide something to test against, lets just attach the context to
    # the object itself
    obj.context = msgpack.loads(frames[1], raw=False)
    return obj


@gen_cluster(
    client=True,
    client_kwargs={"serializers": ["my-ser", "pickle"]},
    worker_kwargs={"serializers": ["my-ser", "pickle"]},
)
async def test_context_specific_serialization(c, s, a, b):
    register_serialization_family("my-ser", my_dumps, my_loads)

    try:
        # Create the object on A, force communication to B
        x = c.submit(MyObject, x=1, y=2, workers=a.address)
        y = c.submit(lambda x: x, x, workers=b.address)

        await wait(y)

        key = y.key

        def check(dask_worker):
            # Get the context from the object stored on B
            my_obj = dask_worker.data[key]
            return my_obj.context

        result = await c.run(check, workers=[b.address])
        expected = {"sender": a.address, "recipient": b.address}
        assert result[b.address]["sender"]["address"] == a.address  # see origin worker

        z = await y  # bring object to local process

        assert z.x == 1 and z.y == 2
        assert z.context["sender"]["address"] == b.address
    finally:
        from distributed.protocol.serialize import families

        del families["my-ser"]


@gen_cluster(client=True)
async def test_context_specific_serialization_class(c, s, a, b):
    register_serialization(MyObject, my_dumps, my_loads)

    # Create the object on A, force communication to B
    x = c.submit(MyObject, x=1, y=2, workers=a.address)
    y = c.submit(lambda x: x, x, workers=b.address)

    await wait(y)

    key = y.key

    def check(dask_worker):
        # Get the context from the object stored on B
        my_obj = dask_worker.data[key]
        return my_obj.context

    result = await c.run(check, workers=[b.address])
    assert result[b.address]["sender"]["address"] == a.address  # see origin worker

    z = await y  # bring object to local process

    assert z.x == 1 and z.y == 2
    assert z.context["sender"]["address"] == b.address


def test_serialize_raises():
    class Foo:
        pass

    @dask_serialize.register(Foo)
    def dumps(f):
        raise Exception("Hello-123")

    with pytest.raises(Exception) as info:
        deserialize(*serialize(Foo()))

    assert "Hello-123" in str(info.value)


@pytest.mark.asyncio
async def test_profile_nested_sizeof():
    # https://github.com/dask/distributed/issues/1674
    n = 500
    original = outer = {}
    inner = {}

    for i in range(n):
        outer["children"] = inner
        outer, inner = inner, {}

    msg = {"data": original}
    frames = await to_frames(msg)


def test_compression_numpy_list():
    class MyObj:
        pass

    @dask_serialize.register(MyObj)
    def _(x):
        header = {"compression": [False]}
        frames = [b""]
        return header, frames

    header, frames = serialize([MyObj(), MyObj()])
    assert header["compression"] == [False, False]


@pytest.mark.parametrize(
    "data,is_serializable",
    [
        ([], False),
        ({}, False),
        ({i: i for i in range(10)}, False),
        (set(range(10)), False),
        (tuple(range(100)), False),
        ({"x": MyObj(5)}, True),
        ({"x": {"y": MyObj(5)}}, True),
        pytest.param(
            [1, MyObj(5)],
            True,
            marks=pytest.mark.xfail(reason="Only checks 0th element for now."),
        ),
        ([MyObj([0, 1, 2]), 1], True),
        (tuple([MyObj(None)]), True),
        ({("x", i): MyObj(5) for i in range(100)}, True),
        (memoryview(b"hello"), True),
        pytest.param(
            memoryview(
                np.random.random((3, 4)) if np is not None else b"skip np.random"
            ),
            True,
            marks=pytest.mark.skipif(np is None, reason="Test needs numpy"),
        ),
    ],
)
def test_check_dask_serializable(data, is_serializable):
    result = check_dask_serializable(data)
    expected = is_serializable

    assert result == expected


@pytest.mark.parametrize(
    "serializers",
    [["msgpack"], ["pickle"], ["msgpack", "pickle"], ["pickle", "msgpack"]],
)
def test_serialize_lists(serializers):
    data_in = ["a", 2, "c", None, "e", 6]
    header, frames = serialize(data_in, serializers=serializers)
    data_out = deserialize(header, frames)

    assert data_in == data_out


@pytest.mark.parametrize(
    "data_in",
    [
        memoryview(b"hello"),
        pytest.param(
            memoryview(
                np.random.random((3, 4)) if np is not None else b"skip np.random"
            ),
            marks=pytest.mark.skipif(np is None, reason="Test needs numpy"),
        ),
    ],
)
def test_deser_memoryview(data_in):
    header, frames = serialize(data_in)
    assert header["type"] == "builtins.memoryview"
    assert frames[0] is data_in
    data_out = deserialize(header, frames)
    assert data_in == data_out


@pytest.mark.skipif(np is None, reason="Test needs numpy")
def test_ser_memoryview_object():
    data_in = memoryview(np.array(["hello"], dtype=object))
    with pytest.raises(TypeError):
        serialize(data_in, on_error="raise")
