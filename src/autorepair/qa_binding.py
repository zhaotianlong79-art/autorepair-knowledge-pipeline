"""Numeric facts bound to explicit source clauses or Markdown row/column scopes.

No unit conversion, invented condition or LLM-derived value is used. Complete
source sections remain the published quotations; bindings identify focused facts.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

from markdown_it import MarkdownIt

from autorepair.qa_language import normalize, relevant


@dataclass(frozen=True)
class Quantity:
    name: str
    intent: str
    units: str


QUANTITIES = (
    Quantity("torque", r"扭矩|力矩", r"N[· ]?m|Nm"),
    Quantity("voltage", r"电压", r"mV|V"),
    Quantity("gap", r"间隙", r"mm|cm"),
    Quantity("thickness", r"厚度|磨损极限", r"mm|cm"),
    Quantity("length", r"长度|直径|下降量", r"mm|cm"),
    Quantity("temperature", r"温度", r"°C|℃"),
    Quantity("pressure", r"轮胎气压|胎压|气压|压力|油压", r"kPa|MPa|bar|psi"),
    Quantity("volume", r"容量|油量|加注量", r"mL|ml|L"),
    Quantity("interval", r"周期|间隔", r"km|公里"),
)
PATTERNS = [q.intent for q in QUANTITIES]
NUMERIC = re.compile(
    r"多少|多大|多高|多长|多薄|多紧|数值|标准|参数|范围|规格|(?:怎么|如何)(?:调|设)|推荐|建议|目标值|几[伏度毫米]"
)
NUMBER = r"[-−]?\d+(?:\.\d+)?(?:\s*[-–—~～至到]\s*[-−]?\d+(?:\.\d+)?)?"
AXES = {
    "axle": {"front": r"前轮|前轴|前制动|左前|右前", "rear": r"后轮|后轴|后制动|左后|右后"},
    "side": {"left": r"左轮|左侧|左前|左后", "right": r"右轮|右侧|右前|右后"},
    "temperature": {"cold": r"冷态|冷车", "hot": r"热态|热车"},
    "load": {"empty": r"空载|轻载", "full": r"满载|重载", "half": r"半载"},
    "engine": {"idle": r"怠速", "high": r"高速"},
}
MD = MarkdownIt("commonmark").enable("table")


def intents(question):
    found = [q for q in QUANTITIES if re.search(q.intent, question)]
    return (
        found
        if NUMERIC.search(question)
        or any(re.search(rf"(?:{q.intent})[？?\s]*$", question) for q in found)
        else []
    )


def conditions(text):
    return {
        axis: {name for name, pattern in labels.items() if re.search(pattern, text)}
        for axis, labels in AXES.items()
    }


def scoped(question, local, section):
    wanted, actual, inherited = conditions(question), conditions(local), conditions(section)
    resolved = {axis: actual[axis] or inherited[axis] for axis in AXES}
    # More than one requested option means an unqualified/both-conditions query;
    # preserve all explicit alternatives so conflicting values cause abstention.
    condition_terms = "|".join(pattern for axis in AXES.values() for pattern in axis.values())
    negated = re.compile(rf"(?:非|不是|不处于|不在)(?:{condition_terms})")
    contradiction = any(
        len(inherited[a]) == 1 and actual[a] and not actual[a].issubset(inherited[a]) for a in AXES
    )
    match = (
        not contradiction
        and not any(negated.search(t) for t in [question, local, section])
        and all(len(wanted[a]) != 1 or resolved[a] == wanted[a] for a in AXES)
    )
    return match, {k: sorted(v) for k, v in resolved.items() if v}


def canonical(number, unit):
    match = re.fullmatch(
        r"([-−]?\d+(?:\.\d+)?)(?:\s*[-–—~～至到]\s*([-−]?\d+(?:\.\d+)?))?", number.strip()
    )
    if match is None:
        raise ValueError("无效参数范围")

    def decimal_text(value):
        raw = format(Decimal(value.replace("−", "-")), "f")
        raw = raw.rstrip("0").rstrip(".") if "." in raw else raw
        return "0" if raw == "-0" else raw

    value = "–".join(decimal_text(v) for v in match.groups() if v is not None)
    unit = re.sub(r"[\s·]", "", unit).replace("℃", "°C").casefold()
    return value + unit


def measurements(text, quantity, header=""):
    unsupported = r"\d,\d|\d\s*/\s*\d|±|\+\s*/\s*-|[<>≤≥≈]|(?:不超过|不低于|不得低于|高于|低于|小于|大于|至少|至多|大约|约)\s*[-−]?\d"
    if re.search(unsupported, text) or re.search(unsupported, header):
        return []
    explicit = list(
        re.finditer(
            rf"(?<![a-z0-9_.−–—-])(?P<number>{NUMBER})\s*(?P<unit>{quantity.units})(?![a-zA-Z])",
            text,
            re.I,
        )
    )
    if explicit:
        header_units = re.findall(rf"(?<![a-zA-Z])(?:{quantity.units})(?![a-zA-Z])", header, re.I)
        if len(header_units) == 1 and any(
            canonical("1", m["unit"]) != canonical("1", header_units[0]) for m in explicit
        ):
            return []
        return [
            (m["number"], m["unit"], "cell" if header else "clause", m.group()) for m in explicit
        ]
    units = list(re.finditer(rf"(?<![a-zA-Z])(?:{quantity.units})(?![a-zA-Z])", header, re.I))
    # Inherit exactly one unit from a column header, never another data row.
    if len(units) == 1 and re.fullmatch(rf"\s*{NUMBER}\s*", text):
        return [(text.strip(), units[0].group(), "column-header", text)]
    return []


def table_rows(text):
    """Read rectangular Markdown tables; ambiguous extra cells are not discarded."""
    tokens = MD.parse(text)
    rows, row, cell = [], None, None
    for token in tokens:
        if token.type == "tr_open":
            row = []
        elif token.type in {"th_open", "td_open"}:
            cell = ""
        elif token.type == "inline" and cell is not None:
            cell += token.content
        elif token.type in {"th_close", "td_close"}:
            row.append(cell)
            cell = None
        elif token.type == "tr_close":
            rows.append(row)
            row = None
    if not rows or any(len(row) != len(rows[0]) for row in rows):
        return []
    raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(raw_lines) != len(rows) + 1:
        return []
    for line in raw_lines:
        fields = re.split(r"(?<!\\)\|", line)
        if fields and not fields[0].strip():
            fields = fields[1:]
        if fields and not fields[-1].strip():
            fields = fields[:-1]
        if len(fields) != len(rows[0]):
            return []
    return rows


def plain(text):
    tokens = MD.parseInline(text)
    return "".join(
        child.content
        if child.type in {"text", "code_inline"}
        else " "
        if child.type in {"softbreak", "hardbreak", "html_inline"}
        else ""
        for token in tokens
        for child in (token.children or [])
    )


def table_topic(question, labels, local, section):
    residual = labels
    for axis in AXES.values():
        for pattern in axis.values():
            residual = re.sub(pattern, "", residual)
    residual = re.sub(r"轮胎|车轮|位置|工况|状态|正常|[\s/、:：|()（）-]", "", residual)
    context = local if residual else local + " " + section
    return relevant(question, normalize(context)[0], PATTERNS)


def row_labels(row, header):
    return " ".join(
        plain(c)
        for i, c in enumerate(row)
        if not re.fullmatch(rf"\s*{NUMBER}\s*", plain(c))
        and not any(measurements(plain(c), q, plain(header[i])) for q in QUANTITIES)
    )


def bind(question, records):
    """Return focused source facts; rows without requested explicit scopes do not bind."""
    wanted = intents(question)
    bindings = []
    for record in records:
        if record["kind"] in {"heading", "picture", "caption"}:
            continue
        section = " / ".join(record.get("section_path", []))
        if record["kind"] == "table":
            rows = table_rows(record["text"])
            if not rows:
                continue
            header = rows[0]
            for row_index, row in enumerate(rows[1:], 1):
                # Other numeric columns cannot donate their conditions/units.
                labels = row_labels(row, header)
                for column_index, cell in enumerate(row):
                    local = labels + " " + plain(header[column_index])
                    match, scope = scoped(question, local, section)
                    if not match:
                        continue
                    for quantity in wanted:
                        if not table_topic(question, labels, local, section):
                            continue
                        dimension_patterns = [
                            q.intent for q in QUANTITIES if q.name in {"gap", "thickness", "length"}
                        ]
                        dimension_label = (
                            local
                            if any(re.search(p, local) for p in dimension_patterns)
                            else local + " " + section
                        )
                        if quantity.name in {"gap", "thickness", "length"} and not re.search(
                            quantity.intent, dimension_label
                        ):
                            continue
                        for number, unit, unit_source, _source in measurements(
                            plain(cell), quantity, plain(header[column_index])
                        ):
                            bindings.append(
                                dict(
                                    block_id=record["block_id"],
                                    quantity=quantity.name,
                                    row=row_index,
                                    column=column_index + 1,
                                    row_label=labels.strip(),
                                    column_label=header[column_index],
                                    source_text=cell,
                                    value=number.strip(),
                                    unit=unit,
                                    unit_source=unit_source,
                                    canonical=canonical(number, unit),
                                    conditions=scope,
                                )
                            )
        else:
            # Commas delimit explicit per-condition clauses, not decimal points.
            for clause in re.split(r"[。；;！？!?\n，,]", record["text"]):
                if not relevant(question, normalize(clause)[0], PATTERNS):
                    continue
                match, scope = scoped(question, clause, section)
                if not match:
                    continue
                for quantity in wanted:
                    for number, unit, unit_source, _source in measurements(clause, quantity):
                        bindings.append(
                            dict(
                                block_id=record["block_id"],
                                quantity=quantity.name,
                                row=None,
                                column=None,
                                row_label="",
                                column_label="",
                                source_text=clause,
                                value=number.strip(),
                                unit=unit,
                                unit_source=unit_source,
                                canonical=canonical(number, unit),
                                conditions=scope,
                            )
                        )
    return bindings


def conflicts(bindings):
    values = {}
    for fact in bindings:
        values.setdefault(fact["quantity"], set()).add(fact["canonical"])
    return any(len(v) > 1 for v in values.values())


def ambiguities(question, records, bindings):
    """Unassigned same-unit prose values cannot silently override a bound fact."""
    output = []
    for record in records:
        if record["kind"] in {"heading", "picture", "caption", "table"}:
            continue
        section = " / ".join(record.get("section_path", []))
        for quantity in intents(question):
            assigned = {
                f["canonical"]
                for f in bindings
                if f["block_id"] == record["block_id"] and f["quantity"] == quantity.name
            }
            if not assigned:
                continue
            for clause in re.split(r"[。；;！？!?\n，,]", record["text"]):
                match, _ = scoped(question, clause, section)
                if not match:
                    continue
                for number, unit, _, _source in measurements(clause, quantity):
                    if canonical(number, unit) not in assigned:
                        output.append(
                            dict(
                                block_id=record["block_id"],
                                quantity=quantity.name,
                                source_text=clause,
                                value=number,
                                unit=unit,
                            )
                        )
    return output


def verify_fact(fact, records, question):
    """Verify literal source cell/clause coordinates and units independently of ranking."""
    try:
        record = next((r for r in records if r["block_id"] == fact["block_id"]), None)
        quantity = next((q for q in QUANTITIES if q.name == fact["quantity"]), None)
        if record is None or quantity is None:
            return False
        section = " / ".join(record.get("section_path", []))
        if fact["row"] is None:
            if (
                record["kind"] == "table"
                or fact["column"] is not None
                or fact["source_text"] not in record["text"]
            ):
                return False
            text, header, local = fact["source_text"], "", fact["source_text"]
            if not relevant(question, normalize(text)[0], PATTERNS):
                return False
        else:
            if (
                record["kind"] != "table"
                or type(fact["row"]) is not int
                or type(fact["column"]) is not int
                or fact["row"] < 1
                or fact["column"] < 1
            ):
                return False
            rows = table_rows(record["text"])
            text, header = rows[fact["row"]][fact["column"] - 1], rows[0][fact["column"] - 1]
            labels = row_labels(rows[fact["row"]], rows[0])
            if (
                text != fact["source_text"]
                or labels != fact["row_label"]
                or header != fact["column_label"]
            ):
                return False
            local = labels + " " + plain(header)
        matched, scope = scoped(question, local, section)
        if not matched or fact["conditions"] != scope:
            return False
        return any(
            fact["value"] == number.strip()
            and fact["unit"] == unit
            and fact["unit_source"] == unit_source
            and fact["canonical"] == canonical(number, unit)
            for number, unit, unit_source, _source in measurements(
                plain(text), quantity, plain(header)
            )
        )
    except (ValueError, KeyError, IndexError, TypeError):
        return False
