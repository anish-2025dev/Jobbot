from app.vault.patch import merge_patch


def test_rfc7386_examples():
    # Selected cases from RFC 7386 appendix A.
    assert merge_patch({"a": "b"}, {"a": "c"}) == {"a": "c"}
    assert merge_patch({"a": "b"}, {"b": "c"}) == {"a": "b", "b": "c"}
    assert merge_patch({"a": "b"}, {"a": None}) == {}
    assert merge_patch({"a": ["b"]}, {"a": "c"}) == {"a": "c"}
    assert merge_patch({"a": "c"}, {"a": ["b"]}) == {"a": ["b"]}
    assert merge_patch({"a": {"b": "c"}}, {"a": {"b": "d", "c": None}}) == {"a": {"b": "d"}}
    assert merge_patch({"a": [{"b": "c"}]}, {"a": [1]}) == {"a": [1]}
    assert merge_patch({"e": None}, {"a": 1}) == {"e": None, "a": 1}
    assert merge_patch([1, 2], {"a": "b", "c": None}) == {"a": "b"}
    assert merge_patch({}, {"a": {"bb": {"ccc": None}}}) == {"a": {"bb": {}}}


def test_does_not_mutate_input():
    target = {"a": {"b": 1}}
    merge_patch(target, {"a": {"b": 2}})
    assert target == {"a": {"b": 1}}
