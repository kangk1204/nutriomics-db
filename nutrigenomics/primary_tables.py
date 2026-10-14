"""Strict parsing of two primary BioC tables; original arm means and SEM kept."""

import re
import xml.etree.ElementTree as ET


def rendered(node):
    result = node.text or ""
    for child in node:
        value = rendered(child)
        result += ("^" + value if child.tag == "sup" else value) + (child.tail or "")
    return " ".join(result.split())


def mean_sem(value):
    if value == "Not detectable":
        return {"value_status": "below_detection", "mean": None, "sem": None, "source_cell": value}
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*±\s*(\d+(?:\.\d+)?)", value)
    if not match:
        raise ValueError("Unrecognized mean/SEM cell: " + value)
    return {
        "value_status": "measured_zero" if float(match[1]) == 0 else "measured",
        "mean": float(match[1]),
        "sem": float(match[2]),
        "source_mean": match[1],
        "source_cell": value,
    }


def tables(raw):
    if len(raw) > 1_000_000:
        raise ValueError("Primary XML cap")
    root = ET.fromstring(raw)
    licenses = [n.text for n in root.findall(".//document/infon") if n.get("key") == "license"]
    selected, captions = {}, {}
    for passage in root.findall(".//passage"):
        info = {n.get("key"): n.text for n in passage.findall("infon")}
        identifier = info.get("id")
        if identifier not in {"T2", "T3"}:
            continue
        if info.get("type") == "table_caption":
            captions[identifier] = passage.findtext("text")
        if info.get("type") == "table":
            if identifier in selected:
                raise ValueError("Duplicate primary table")
            selected[identifier] = ET.fromstring(info["xml"])
    if set(selected) != {"T2", "T3"} or set(captions) != {"T2", "T3"}:
        raise ValueError("Expected two complete primary tables with captions")
    if (
        "SEM" not in captions["T2"]
        or "n=11" not in captions["T2"]
        or "n=10" not in captions["T3"]
        or "n=11" not in captions["T3"]
    ):
        raise ValueError("Primary denominator/uncertainty mismatch")
    result = []
    for identifier, table in selected.items():
        headings = [rendered(cell) for cell in table.findall("./thead/tr/th")]
        if headings != ["", "Placebo", "Resveratrol", "p-value"]:
            raise ValueError("Arm or comparison-column mismatch")
        labels = set()
        for index, row in enumerate(table.findall("./tbody/tr")):
            cells = [rendered(cell) for cell in row.findall("td")]
            if len(cells) != 4 or cells[0] in labels:
                raise ValueError("Duplicate endpoint or inconsistent row shape")
            labels.add(cells[0])
            match = re.fullmatch(r"(.+) \(([^()]*)\)", cells[0])
            label, unit = match.groups() if match else (cells[0], "dimensionless source index")
            n = (
                11
                if identifier == "T2"
                or "blood pressure" in label.lower()
                or "arterial pressure" in label.lower()
                else 10
            )
            p = None if cells[3] == "-" else float(cells[3])
            if p is not None and not 0 <= p <= 1:
                raise ValueError("Invalid source p-value")
            result.append(
                {
                    "table": identifier,
                    "row_index": index + 1,
                    "source_label": cells[0],
                    "label": label,
                    "unit": unit,
                    "n_people": n,
                    "placebo": mean_sem(cells[1]),
                    "resveratrol": mean_sem(cells[2]),
                    "p_value": p,
                    "caption": captions[identifier],
                    "source_license_metadata": licenses,
                }
            )
    return result
