"""Split a SQL script into single statements, keeping $$ bodies intact.

One statement per execute: psycopg 3's extended protocol rejects
multi-statement strings whenever a parameter object is passed. Used by
migrations 0002+ (0001 keeps its own frozen copy).
"""


def split_sql(sql: str) -> list[str]:
    stmts, buf, in_dollar = [], [], False
    for line in sql.splitlines():
        code = line if in_dollar else line.split("--", 1)[0]
        buf.append(line)
        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar
        if not in_dollar and code.rstrip().endswith(";"):
            while buf and (not buf[0].strip() or buf[0].strip().startswith("--")):
                buf.pop(0)
            if buf:
                stmts.append("\n".join(buf).strip())
            buf = []
    leftover = [l for l in buf if l.strip() and not l.strip().startswith("--")]
    if in_dollar or leftover:
        raise ValueError(f"unterminated SQL near: {leftover[:3]}")
    return stmts
