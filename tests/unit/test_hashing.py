import pytest

from app.util.hashing import (canonical_hash, company_key, fingerprint, h64,
                              hamming, normalize_location, normalize_title,
                              simhash)

JD = """We are hiring a backend engineer to join the payments platform team in Bengaluru.
You will design and build high throughput services in Go and Python, own APIs used by
millions of merchants, and work closely with product and infrastructure teams. Our stack
includes Postgres, Kafka, Redis, Kubernetes and AWS. Responsibilities include writing clean
tested code, reviewing pull requests, participating in on call rotations, improving
observability with metrics and tracing, and mentoring interns. Requirements: a degree in
computer science or equivalent experience, strong fundamentals in data structures and
algorithms, familiarity with relational databases and SQL, understanding of REST and gRPC,
and good written communication. Nice to have: experience with distributed systems,
event driven architecture, message queues, containerization, CI/CD pipelines, and cloud
cost optimization. We offer competitive pay, flexible hybrid work, health insurance,
a learning budget, and a team that values ownership and curiosity. Freshers graduating
in 2026 or 2027 with strong projects are encouraged to apply. The interview process has
an online assessment, two technical rounds and a hiring manager conversation."""


# --- h64 ---------------------------------------------------------------------
def test_h64_fits_bigint():
    for s in ["", "a", "razorpay", "x" * 10_000, "दिल्ली"]:
        assert -(2**63) <= h64(s) < 2**63


def test_h64_deterministic():
    assert h64("greenhouse|stripe|4512") == h64("greenhouse|stripe|4512")
    assert h64("a") != h64("b")


def test_h64_known_vector():
    # xxh64("") = 0xEF46DB3751D8E999 (reference value), stored signed.
    assert h64("") == 0xEF46DB3751D8E999 - (1 << 64)


# --- company_key (C5/C9: caps key on the employer) ----------------------------
@pytest.mark.parametrize("raw,key", [
    ("Razorpay Software Pvt. Ltd.", "razorpay software"),
    ("Infosys Limited", "infosys"),
    ("Stripe, Inc.", "stripe"),
    ("Google India Pvt Ltd", "google"),
    ("Microsoft India (R&D) Private Limited", "microsoft"),
    ("Zeta Technologies", "zeta technologies"),   # distinguishing word kept
    ("Global", "global"),                         # single word never stripped
    ("  ACME   corp ", "acme"),
])
def test_company_key(raw, key):
    assert company_key(raw) == key


def test_company_key_same_employer_across_sources():
    assert company_key("Google") == company_key("Google India Private Limited")


# --- titles / locations / fingerprint (C4 cross-portal) ------------------------
@pytest.mark.parametrize("raw,norm", [
    ("Software Engineer I (R123456)", "software engineer i"),
    ("SDE-1 [JR-10234]", "sde 1"),
    ("Backend Engineer Req#4512", "backend engineer"),
    ("Data Engineer 1234567", "data engineer"),
    ("SDE Intern 2026", "sde intern 2026"),       # years are NOT req codes
    ("Graduate Engineer Trainee - 2027 Batch", "graduate engineer trainee 2027 batch"),
])
def test_normalize_title(raw, norm):
    assert normalize_title(raw) == norm


def test_years_do_not_collide():
    a = fingerprint("Acme", "SDE Intern 2026", "Delhi")
    b = fingerprint("Acme", "SDE Intern 2027", "Delhi")
    assert a != b


@pytest.mark.parametrize("a,b", [
    ("Bangalore, India", "Bengaluru, India"),
    ("Gurgaon", "Gurugram"),
    ("New Delhi", "Delhi"),
])
def test_city_aliases(a, b):
    assert normalize_location(a) == normalize_location(b)


def test_fingerprint_matches_across_portals():
    a = fingerprint("Razorpay Pvt Ltd", "SDE-1 (JR10234)", "Bangalore")
    b = fingerprint("razorpay", "SDE 1", "Bengaluru")
    assert a == b


def test_fingerprint_differs_by_location():
    assert fingerprint("Acme", "SDE", "Delhi") != fingerprint("Acme", "SDE", "Pune")


# --- canonical_hash -------------------------------------------------------------
def test_canonical_prefers_req_id_over_url():
    a = canonical_hash("greenhouse", "Stripe", "4512", "https://x/jobs/4512?gh_src=a")
    b = canonical_hash("greenhouse", "Stripe, Inc.", "4512", "https://other")
    assert a == b


def test_canonical_url_fallback_ignores_tracking():
    a = canonical_hash("jsonld", "Acme", None, "https://acme.com/jobs/77?utm_source=x")
    b = canonical_hash("jsonld", "Acme", None, "https://acme.com/jobs/77#apply")
    assert a == b


def test_canonical_scoped_by_ats_and_company():
    assert canonical_hash("lever", "A", "1", "u") != canonical_hash("ashby", "A", "1", "u")
    assert canonical_hash("lever", "A", "1", "u") != canonical_hash("lever", "B", "1", "u")


# --- simhash (repost detection, section 6) --------------------------------------
def test_simhash_detects_light_edit_repost():
    repost = JD.replace("We are hiring", "We're hiring").replace("Bengaluru", "Bangalore")
    assert hamming(simhash(JD), simhash(repost)) <= 6


def test_simhash_separates_different_jd():
    other = JD.replace("backend engineer", "data scientist").replace("Go and Python", "R and SQL")
    other = " ".join(other.split()[::-1])
    assert hamming(simhash(JD), simhash(other)) > 12


def test_simhash_short_and_empty_text():
    for t in ["", "a", "two words"]:
        assert -(2**63) <= simhash(t) < 2**63


def test_hamming_basics():
    assert hamming(0, 0) == 0
    assert hamming(-1, 0) == 64
    assert hamming(5, 5) == 0
