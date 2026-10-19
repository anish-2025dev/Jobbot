import pytest

from app.vault.facts import VaultFacts, extract_numbers, norm_skill


@pytest.mark.parametrize("text,nums", [
    ("raised coverage from 45% to 82%", ["45%", "82%"]),
    ("handling 40k requests/day", ["40000"]),
    ("1,200 users and 1.2k users", ["1200", "1200"]),
    ("1,00,000 orders", ["100000"]),
    ("3.5M rows", ["3500000"]),
    ("3,500,000 rows", ["3500000"]),
    ("a 10x speedup and 10 X more", ["10x", "10x"]),
    ("900 ms to 350 ms", ["900", "350"]),
    ("₹12 LPA expected", ["12"]),
    ("0.91 F1 score", ["0.91"]),
    ("400+ problems", ["400"]),
    ("Python3 and v2 API", []),             # digits glued to words aren't metrics
    ("no numbers here", []),
    ("", []),
])
def test_extract_numbers(text, nums):
    assert extract_numbers(text) == nums


def test_equivalent_forms_match():
    assert extract_numbers("1.2k")[0] == extract_numbers("1,200")[0] == extract_numbers("1200")[0]
    assert extract_numbers("1.20")[0] == extract_numbers("1.2")[0]


@pytest.mark.parametrize("raw,norm", [
    ("Postgres", "postgresql"), ("  K8s ", "kubernetes"), ("NodeJS", "node.js"),
    ("Golang", "go"), ("React.js", "react"), ("C Sharp", "c#"),
    ("FastAPI", "fastapi"), ("Machine   Learning", "machine learning"),
])
def test_norm_skill(raw, norm):
    assert norm_skill(raw) == norm


def test_untraceable_helpers():
    f = VaultFacts(skills={"python", "postgresql"}, numbers={"45%", "40000"})
    assert f.untraceable_numbers("served 40k users, up 45%") == []
    assert f.untraceable_numbers("served 50k users, up 45%") == ["50000"]
    assert f.untraceable_skills(["Postgres", "Python", "Kafka"]) == ["Kafka"]
