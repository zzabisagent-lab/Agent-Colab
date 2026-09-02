"""RFC 8785 conformance (appendix examples) and negative cases."""

from __future__ import annotations

import pytest

from server.events.canonical import CanonicalizationError, canonical_json


def test_rfc8785_object_example() -> None:
    value = {
        "numbers": [333333333.33333329, 1e30, 4.5, 2e-3, 0.000000000000000000000000001],
        "string": '€$\u000f\nA\'B"\\\\"/',
        "literals": [None, True, False],
    }
    expected = (
        '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    ).encode()
    assert canonical_json(value) == expected


def test_key_sorting_by_utf16_code_units() -> None:
    value = {"€": 1, "\U0001f600": 2, "a": 3, "A": 4, "é": 5}
    # UTF-16 order: A(0041) a(0061) é(00e9) €(20ac) 😀(d83d de00)
    assert canonical_json(value) == '{"A":4,"a":3,"é":5,"€":1,"\U0001f600":2}'.encode()


@pytest.mark.parametrize(
    ("number", "text"),
    [
        (0.0, "0"),
        (1.0, "1"),
        (1e21, "1e+21"),
        (1e20, "100000000000000000000"),
        (1e-7, "1e-7"),
        (0.000001, "0.000001"),
        (5e-324, "5e-324"),
        (-1.5, "-1.5"),
        (123456789012345680000.0, "123456789012345680000"),
        (0.1, "0.1"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
    ],
)
def test_es6_number_formatting(number: float, text: str) -> None:
    assert canonical_json(number).decode() == text


def test_integers_and_nested_arrays() -> None:
    value = {"b": [1, {"y": 2, "x": [3]}], "a": -0}
    assert canonical_json(value) == b'{"a":0,"b":[1,{"x":[3],"y":2}]}'


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), {1: "x"}, {"a": {1, 2}}, b"bytes"])
def test_rejects_non_canonical_values(bad: object) -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json(bad)


def test_deterministic_across_insertion_order() -> None:
    assert canonical_json({"z": 1, "a": 2}) == canonical_json({"a": 2, "z": 1})
