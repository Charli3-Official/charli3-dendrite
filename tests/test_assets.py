"""Contract tests for the plain (non-pydantic) ``Assets``.

``Assets`` is a plain ``unit -> quantity`` mapping (lovelace-first) with a live,
mutable ``.root`` dict that pool math mutates in place. These tests pin the full
surface consumers rely on so the pydantic-free implementation stays a
drop-in: construction shapes, ordering, accessors, arithmetic, the pydantic-model
API (model_dump / model_validate / model_construct / copy), the non-ADA in-place
``.root`` mutation, and use as a field type inside a pydantic model.
"""

import json

from pydantic import BaseModel

from charli3_dendrite.dataclasses.models import Assets

L = "lovelace"
T1 = "aa11" + "11" * 26  # policy(56)+name; T1 < T2 lexicographically
T2 = "bb22" + "22" * 26
T3 = "cc33" + "33" * 26


def test_lovelace_first_ordering():
    a = Assets(root={T2: 5, L: 7, T1: 3})
    assert list(a.keys()) == [L, T1, T2]


def test_construction_shapes_agree():
    ref = [(L, 7), (T1, 3)]
    assert list(Assets(root={T1: 3, L: 7}).items()) == ref
    assert list(Assets(**{T1: 3, L: 7}).items()) == ref
    assert list(Assets({T1: 3, L: 7}).items()) == ref
    assert list(Assets([{T1: 3}, {L: 7}]).items()) == ref
    assert list(Assets(Assets(root={T1: 3, L: 7})).items()) == ref
    assert list(Assets(root={}).items()) == []


def test_accessors():
    a = Assets(root={T2: 5, L: 7, T1: 3})
    assert a.unit() == L and a.unit(1) == T1 and a.unit(2) == T2
    assert a.quantity() == 7 and a.quantity(1) == 3
    assert a[T1] == 3
    assert a["not-present"] == 0  # missing -> 0
    assert len(a) == 3
    assert L in a and "nope" not in a
    assert list(iter(a)) == [L, T1, T2]


def test_arithmetic():
    a = Assets(root={L: 7, T1: 3})
    b = Assets(root={T1: 10, T3: 1})
    assert dict((a + b).items()) == {L: 7, T1: 13, T3: 1}
    assert dict((a - b).items()) == {L: 7, T1: -7, T3: -1}  # goes negative


def test_hash_eq_consistency():
    a = Assets(root={T2: 5, L: 7})
    b = Assets(root={L: 7, T2: 5})
    assert a == b
    assert hash(a) == hash(b)
    assert a != Assets(root={L: 7})
    assert a != {"lovelace": 7}  # not equal to a bare dict


def test_pydantic_model_api():
    a = Assets(root={T2: 5, L: 7})
    assert a.model_dump() == {L: 7, T2: 5}
    assert json.loads(a.model_dump_json()) == {L: 7, T2: 5}
    assert list(Assets.model_validate({T1: 3, L: 1}).items()) == [(L, 1), (T1, 3)]
    assert list(Assets.model_validate_json('{"lovelace": 1}').items()) == [(L, 1)]
    # copy is independent and order-preserving
    cp = a.copy()
    cp.root[T2] = 999
    assert a.root[T2] == 5


def test_model_construct_does_not_sort():
    # reset_assets passes an already-canonical root and must NOT be re-sorted.
    c = Assets.model_construct(root={T2: 5, L: 7})
    assert list(c.keys()) == [T2, L]  # verbatim, lovelace NOT moved first


def test_non_ada_pool_in_place_mutation():
    # Non-ADA pool init builds {T1, T2} then appends a synthetic min-ADA IN PLACE,
    # which must NOT re-sort (keeps the two real tokens at index 0/1). apply_swap
    # then mutates a reserve in place.
    a = Assets(root={T1: 1000, T2: 2000})
    assert list(a.keys()) == [T1, T2]
    a.root[L] = 2_000_000
    assert list(a.keys()) == [T1, T2, L]  # appended at index 2, not re-sorted
    assert a.unit(0) == T1 and a.unit(1) == T2  # dendrite reads unit_a/unit_b here
    a.root[T1] += 500
    assert a.root[T1] == 1500


def test_usable_as_pydantic_field():
    class Model(BaseModel):
        model_config = {"arbitrary_types_allowed": True}
        assets: Assets

    m = Model(assets={T2: 5, L: 7})
    assert list(m.assets.keys()) == [L, T2]
    assert m.model_dump() == {"assets": {L: 7, T2: 5}}
    assert json.loads(m.model_dump_json()) == {"assets": {L: 7, T2: 5}}
    # validate from an existing instance and from JSON
    assert Model(assets=Assets(root={L: 9})).assets.root == {L: 9}
    rt = Model.model_validate_json(json.dumps({"assets": {T1: 3, L: 1}}))
    assert list(rt.assets.items()) == [(L, 1), (T1, 3)]
