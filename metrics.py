import re
from models import SnapshotMetric

VALUE_RE = re.compile(r"(?P<unit>[$€£₹])?\s*(?P<value>-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)(?P<suffix>\s*%|\s*(?:USD|EUR|GBP|INR|kg|g|mg|km|mi|cm|mm|GB|MB|TB|users|items|units|days|hours))?", re.I)
DATE_RE = re.compile(r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\b")


def extract_metrics(text):
    found = {}
    for line in (text or "").splitlines():
        clean = " ".join(line.split())
        if not clean or len(clean) > 500 or DATE_RE.search(clean):
            continue
        matches = list(VALUE_RE.finditer(clean))
        if not matches:
            continue
        match = matches[-1]
        raw = match.group("value").replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        if abs(value) > 1e15:
            continue
        label = clean[:match.start()].strip(" :-|·\t")
        label = re.sub(r"https?://\S+", "", label).strip(" :-|·\t")
        if not label:
            label = clean[match.end():].strip(" :-|·\t") or "Value"
        label = re.sub(r"\s+", " ", label)[:150]
        if len(label) < 2 or not re.search(r"[A-Za-z]", label):
            continue
        unit = (match.group("unit") or "").strip() + (match.group("suffix") or "").strip()
        key = (label.casefold(), unit.casefold())
        found[key] = (label, value, unit, clean[:500])
    return list(found.values())


def save_snapshot_metrics(snapshot, text):
    for name, value, unit, context in extract_metrics(text):
        snapshot.metrics.append(SnapshotMetric(name=name, value=value, unit=unit, context=context))
