"""Tech-skill lexicon, alias folding and extraction from free text.

Used for: JD skill extraction (scoring, C3 summaries), resume keyword
mirroring, and the truth validator (C11), which flags any lexicon skill that
appears in rewritten text but not in the vault.

Short or common-word skills (Go, R, C, Rust, Swift, Spark...) are matched
case-sensitively in tech-looking contexts only, so "go to market" or
"spark joy" never count as skills.
"""
from __future__ import annotations

import re
from functools import lru_cache

ALIASES = {
    "js": "javascript", "ts": "typescript", "py": "python", "golang": "go",
    "node": "node.js", "nodejs": "node.js", "node js": "node.js",
    "reactjs": "react", "react.js": "react", "react js": "react",
    "nextjs": "next.js", "next js": "next.js", "vuejs": "vue", "vue.js": "vue",
    "angularjs": "angular", "expressjs": "express", "express.js": "express",
    "nestjs": "nest.js", "nest js": "nest.js",
    "postgres": "postgresql", "psql": "postgresql", "pg": "postgresql",
    "mongo": "mongodb", "mssql": "sql server", "ms sql": "sql server",
    "k8s": "kubernetes", "gcp": "google cloud", "google cloud platform": "google cloud",
    "amazon web services": "aws", "microsoft azure": "azure",
    "c sharp": "c#", "csharp": "c#", "dotnet": ".net", "dot net": ".net",
    ".net core": ".net", "asp.net": "asp.net core", "asp .net": "asp.net core",
    "sklearn": "scikit-learn", "scikit learn": "scikit-learn",
    "ml": "machine learning", "dl": "deep learning", "dsa": "data structures and algorithms",
    "data structures": "data structures and algorithms", "algorithms": "data structures and algorithms",
    "oop": "object-oriented programming", "oops": "object-oriented programming",
    "object oriented programming": "object-oriented programming",
    "restful": "rest apis", "rest": "rest apis", "rest api": "rest apis", "restful apis": "rest apis",
    "restful api": "rest apis", "grpc": "grpc", "ci/cd": "ci/cd", "cicd": "ci/cd", "ci cd": "ci/cd",
    "github action": "github actions", "tf": "terraform", "elastic search": "elasticsearch",
    "elk": "elasticsearch", "open search": "opensearch", "llms": "llm",
    "large language models": "llm", "gen ai": "generative ai", "genai": "generative ai",
    "nlp": "natural language processing", "cv": "computer vision",
    "pytorch lightning": "pytorch", "tensorflow2": "tensorflow", "hf": "hugging face",
    "huggingface": "hugging face", "power-bi": "power bi", "powerbi": "power bi",
    "ms excel": "excel", "springboot": "spring boot", "spring-boot": "spring boot",
    "rabbit mq": "rabbitmq", "apache kafka": "kafka", "apache spark": "spark",
    "pyspark": "spark", "apache airflow": "airflow", "dbt core": "dbt",
    "system design": "system design", "distributed system": "distributed systems",
    "microservice": "microservices", "micro services": "microservices",
    "unit tests": "unit testing", "unit test": "unit testing",
    "tailwindcss": "tailwind", "tailwind css": "tailwind", "html5": "html", "css3": "css",
    "shell scripting": "bash", "shell": "bash", "linux/unix": "linux", "unix": "linux",
    "mysql db": "mysql", "redis cache": "redis", "fast api": "fastapi",
    "web sockets": "websockets", "websocket": "websockets", "socket.io": "websockets",
}

# Canonical lowercase names. Grouped only for readability.
LEXICON = sorted(set("""
python|java|javascript|typescript|go|rust|c|c++|c#|kotlin|swift|scala|ruby|php|r|dart|elixir|
haskell|perl|lua|matlab|objective-c|solidity|sql|bash|powershell|
react|angular|vue|svelte|next.js|nuxt|redux|html|css|sass|tailwind|bootstrap|jquery|webpack|vite|
node.js|express|nest.js|django|flask|fastapi|spring|spring boot|hibernate|.net|asp.net core|
rails|laravel|gin|fiber|actix|graphql|rest apis|grpc|websockets|oauth|jwt|
postgresql|mysql|mariadb|sqlite|sql server|oracle|mongodb|redis|cassandra|dynamodb|couchbase|
elasticsearch|opensearch|neo4j|clickhouse|snowflake|bigquery|redshift|pgvector|pinecone|
kafka|rabbitmq|activemq|sqs|pub/sub|celery|airflow|dbt|spark|hadoop|flink|beam|databricks|
pandas|numpy|scipy|matplotlib|power bi|tableau|looker|excel|mlflow|kubeflow|
machine learning|deep learning|natural language processing|computer vision|generative ai|llm|
pytorch|tensorflow|keras|scikit-learn|xgboost|lightgbm|hugging face|langchain|llamaindex|opencv|
docker|kubernetes|helm|terraform|ansible|jenkins|github actions|gitlab ci|circleci|argo cd|
aws|azure|google cloud|lambda|ec2|s3|cloudformation|serverless|nginx|apache|linux|git|
prometheus|grafana|datadog|new relic|sentry|opentelemetry|splunk|elk|
selenium|playwright|cypress|jest|mocha|pytest|junit|xunit|nunit|testng|postman|
unit testing|integration testing|tdd|ci/cd|microservices|distributed systems|system design|
data structures and algorithms|object-oriented programming|design patterns|agile|scrum|jira|
android|ios|react native|flutter|swiftui|jetpack compose|xamarin|
wireshark|scapy|zeek|snort|suricata|nmap|burp suite|metasploit|siem|owasp|
penetration testing|cryptography|webrtc|unity|unreal engine|blockchain|ethereum|
figma|photoshop|sap|salesforce|servicenow
""".replace("\n", "").split("|")) - {""})

# Matched case-sensitively, only in tech-looking contexts.
_AMBIGUOUS = {
    "go": re.compile(r"(?<![\w.])(?:Go(?:lang)?|golang|GoLang|GOLANG)(?![\w#+])"),
    "r": re.compile(r"(?<![\w.])R(?=\s*(?:,|/|\)|;| and | or |programming|language|studio|$))"),
    "c": re.compile(r"(?<![\w.#+])C(?=\s*(?:,|/|\)|;| and | or |programming|language|\+\+|$))(?!\+\+|#)"),
    "rust": re.compile(r"(?<![\w.])Rust(?![\w])"),
    "swift": re.compile(r"(?<![\w.])Swift(?![\w])"),
    "spark": re.compile(r"(?<![\w.])(?:Apache Spark|Spark|PySpark)(?![\w])"),
    "dart": re.compile(r"(?<![\w.])Dart(?![\w])"),
    "spring": re.compile(r"(?<![\w.])Spring(?: Boot| Framework| MVC)?(?![\w])"),
    "express": re.compile(r"(?<![\w.])Express(?:\.js|JS)?(?![\w])"),
    "apache": re.compile(r"(?<![\w.])Apache(?! (?:Spark|Kafka|Airflow|Flink|Beam))(?![\w])"),
    "lambda": re.compile(r"(?<![\w.])(?:AWS Lambda|Lambda)(?![\w])"),
    "excel": re.compile(r"(?<![\w.])(?:MS Excel|Excel)(?![\w])"),
    "oracle": re.compile(r"(?<![\w.])Oracle(?![\w])"),
    "git": re.compile(r"(?<![\w.])(?:Git|git)(?![\w])"),
    "beam": re.compile(r"(?<![\w.])Apache Beam(?![\w])"),
    "unity": re.compile(r"(?<![\w.])Unity(?:3D)?(?![\w])"),
    "sap": re.compile(r"(?<![\w.])SAP(?![\w])"),
    "elk": re.compile(r"(?<![\w.])ELK(?![\w])"),
    "gin": re.compile(r"(?<![\w.])Gin(?![\w])"),
    "fiber": re.compile(r"(?<![\w.])Fiber(?![\w])"),
    "flask": re.compile(r"(?<![\w.])Flask(?![\w])"),
    "helm": re.compile(r"(?<![\w.])Helm(?![\w])"),
    "jest": re.compile(r"(?<![\w.])Jest(?![\w])"),
    "redux": re.compile(r"(?<![\w.])Redux(?![\w])"),
    "scrum": re.compile(r"(?<![\w.])Scrum(?![\w])"),
}


def norm_skill(skill: str) -> str:
    key = re.sub(r"\s+", " ", (skill or "").strip().casefold())
    return ALIASES.get(key, key)


def _key(text: str) -> str:
    return re.sub(r"[\s\-]+", " ", text.casefold()).strip()


@lru_cache(maxsize=16)
def _combined(extra: tuple[str, ...] = ()) -> tuple[re.Pattern, dict[str, str]]:
    """One alternation over every unambiguous term (longest first, so
    'spring boot' wins over 'spring' and 'sql server' over 'sql'), plus a
    lookup from the matched text to its canonical name."""
    canon: dict[str, str] = {}
    for t in set(LEXICON) | set(ALIASES):
        if t not in _AMBIGUOUS and norm_skill(t) not in _AMBIGUOUS:
            canon[_key(t)] = norm_skill(t)
    for e in extra:
        n = norm_skill(e)
        if n and n not in _AMBIGUOUS:
            canon[_key(e)] = n
            canon[_key(n)] = n
    alts = sorted(canon, key=len, reverse=True)
    body = "|".join(re.escape(a).replace(r"\ ", r"[\s\-]+") for a in alts)
    pat = re.compile(r"(?<![a-z0-9+#.])(?:" + body + r")(?![a-z0-9+#]|\.[a-z0-9])")
    return pat, canon


def skill_spans(text: str, extra: tuple[str, ...] = ()) -> list[tuple[str, int]]:
    """(canonical skill, offset) for every mention, in text order."""
    if not text:
        return []
    pat, canon = _combined(tuple(sorted(set(extra))))
    low = text.lower()
    spans = [(canon.get(_key(m.group(0)), _key(m.group(0))), m.start()) for m in pat.finditer(low)]
    for c, apat in _AMBIGUOUS.items():
        spans += [(c, m.start()) for m in apat.finditer(text)]
    return sorted(spans, key=lambda t: t[1])


def extract_skills(text: str, extra: tuple[str, ...] = ()) -> list[str]:
    """Canonical skills mentioned in `text`, in first-seen order.

    `extra` adds user-specific terms (vault skills) to the lexicon so niche
    tools the user lists are detected too."""
    return list(dict.fromkeys(c for c, _ in skill_spans(text, extra)))


_REQ_HEADINGS = re.compile(
    r"(requirements|qualifications|what you('| wi)ll (need|bring)|must have|you have|"
    r"who you are|basic qualifications|minimum qualifications|skills)", re.I)
_NICE_HEADINGS = re.compile(r"(nice to have|preferred|bonus|good to have|plus)", re.I)


def must_have_skills(jd_text: str, extra: tuple[str, ...] = ()) -> tuple[list[str], list[str]]:
    """(must-have, nice-to-have) skills. Everything is must-have except
    lines under a 'nice to have / preferred / bonus' heading. One scan of
    the text; mentions are assigned to sections by offset."""
    text = jd_text or ""
    sections: list[tuple[int, str]] = [(0, "other")]
    offset = 0
    for line in text.split("\n"):
        stripped = line.strip("-•*: ").strip()
        if len(stripped) < 60 and _NICE_HEADINGS.search(stripped):
            sections.append((offset, "nice"))
        elif len(stripped) < 60 and _REQ_HEADINGS.search(stripped):
            sections.append((offset, "must"))
        offset += len(line) + 1
    req: list[str] = []
    body: list[str] = []
    nice: list[str] = []
    idx = 0
    for canon, pos in skill_spans(text, extra):
        while idx + 1 < len(sections) and sections[idx + 1][0] <= pos:
            idx += 1
        {"must": req, "other": body, "nice": nice}[sections[idx][1]].append(canon)
    # requirement-section skills first, then the description body
    must_set = list(dict.fromkeys(req + body))
    nice_set = [x for x in dict.fromkeys(nice) if x not in must_set]
    return must_set, nice_set
