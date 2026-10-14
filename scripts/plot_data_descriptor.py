"""Publication figures from exact candidate counts and validation receipts."""
import argparse
import csv
import json
import shutil
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

COLORS = {"food": "#bd6d3d", "omics": "#56649a", "curation": "#347d78", "neutral": "#60747d"}


def render(metadata, review_manifest, performance, output):
    metadata, review_manifest, output = Path(metadata), Path(review_manifest), Path(output)
    data = json.loads(metadata.read_text(encoding="utf-8"))
    reviews = json.loads(review_manifest.read_text(encoding="utf-8"))
    metrics = json.loads(Path(performance).read_text()) if performance else None
    counts = data["counts"]
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "svg.fonttype": "none", "pdf.fonttype": 42, "axes.spines.top": False, "axes.spines.right": False})
    packages = []
    def save(fig, number, header, rows, legend):
        path = output / ("F" + str(number))
        path.mkdir(parents=True, exist_ok=True)
        for ext in ("png", "pdf", "svg"):
            fig.savefig(path / f"F{number}.{ext}", dpi=300, facecolor="white", bbox_inches="tight")
        with (path / "source-data.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f); writer.writerow(header); writer.writerows(rows)
        shutil.copyfile(metadata, path / "dataset-metadata.json")
        shutil.copyfile(review_manifest, path / "review-manifest.json")
        shutil.copyfile(__file__, path / "plot_data_descriptor.py")
        if performance:
            shutil.copyfile(performance, path / "performance.json")
        (path / "legend.txt").write_text(legend + "\nRelease: " + data["release_id"] + ". Candidate; scientific review pending.\n", encoding="utf-8")
        (path / "regenerate.json").write_text(json.dumps({"command": "python plot_data_descriptor.py --metadata dataset-metadata.json --review-manifest review-manifest.json --output regenerated" + (" --performance performance.json" if performance else ""), "matplotlib": matplotlib.__version__, "statistical_inference": "none", "count_units": data["count_definitions"]}, indent=2), encoding="utf-8")
        packages.append(str(path))
        plt.close(fig)
    # Figure 1: schema and processing chain; no biological mechanism is asserted.
    fig, ax = plt.subplots(figsize=(7.2, 3.5)); ax.set(xlim=(0, 10), ylim=(0, 4)); ax.axis("off")
    blocks = [(0.1, 2.3, "Food composition", "food", "Source food ID, state\nquantity, unit, basis"),
              (3.35, 2.3, "Human intervention", "curation", "Exposure, arm, contrast\ntissue, time, participants"),
              (6.6, 2.3, "Omics results", "omics", "Five-part result key\neffect, uncertainty, P / FDR"),
              (3.35, 0.25, "Versioned release", "neutral", "SQLite · Parquet · CSV\nmanifest, policy, review")]
    for x, y, title, color, text in blocks:
        ax.add_patch(FancyBboxPatch((x, y), 3, 1.2, boxstyle="round,pad=0.04,rounding_size=.05", facecolor="#f5f7f8", edgecolor=COLORS[color], linewidth=1))
        ax.text(x + .15, y + .94, title, fontweight="bold", color=COLORS[color]); ax.text(x + .15, y + .57, text, va="top", linespacing=1.5)
    for x1, y1, x2, y2 in [(3.15, 2.9, 3.3, 2.9), (6.4, 2.9, 6.55, 2.9), (4.85, 2.2, 4.85, 1.55)]:
        ax.annotate("", (x2, y2), (x1, y1), arrowprops={"arrowstyle": "->", "color": COLORS["neutral"]})
    ax.text(.1, 1.8, "Related food/component mapping;\ndose equivalence is checked separately", fontsize=8)
    ax.text(6.65, 1.8, "Observed study results;\nno individual health prediction", fontsize=8)
    ax.text(.1, .3, "Source rights\n+ content hashes", color=COLORS["neutral"])
    ax.text(6.65, .3, "Human scientific review\n+ technical validation", color=COLORS["neutral"])
    save(fig, 1, ["entity", "fields"], [(b[2], b[4].replace('\n', '; ')) for b in blocks], "Figure 1. Candidate data model and release workflow. Arrows represent database linkage and processing, not causal biological effects.")
    # Figure 2: distinct grains; composition relations and omics records are not pooled.
    fig, axes = plt.subplots(1, 3, figsize=(7.2, 3.2), gridspec_kw={"wspace": .7})
    sets = [list(counts["node_kinds"].items()), list(counts["science_kinds"].items()), list(counts["effect_types"].items())]
    plotted = []
    for panel, ax, entries, title, color in zip("ABC", axes, sets, ["Entities", "Curation records", "Quantitative results"], ["food", "curation", "omics"], strict=True):
        entries = [(k, v) for k, v in entries if k != "curation"]
        labels, values = zip(*entries) if entries else ([], [])
        ax.barh(range(len(values)), values, color=COLORS[color], height=.65)
        ax.set_yticks(range(len(values)), [x.replace('_', '\n') for x in labels], fontsize=7)
        ax.invert_yaxis(); ax.set_title(panel + "  " + title, loc="left", fontsize=10)
        ax.ticklabel_format(axis="x", style="sci", scilimits=(0, 4)); ax.set_xlabel("Records", fontsize=8)
        plotted.extend((panel, k, v) for k, v in entries)
    save(fig, 2, ["panel", "type", "records"], plotted, "Figure 2. Actual public-candidate counts by data grain. Entity namespaces are not unique biological genes; quantitative records are feature × contrast × analysis-version results, not participants. External unassessed values are excluded.")
    # Figure 3: explicitly stated use cases, supported by the requested case design.
    fig, ax = plt.subplots(figsize=(7.2, 3.5)); ax.axis("off")
    examples = [("Yogurt", "Input: 150 g", "Acute study: 800 g", "Dose mismatch; product identity unverified"),
                ("Fatty-acid meal", "Food / component query", "PUFA vs SFA, 6 h", "Compare study changes; retain PBMC context"),
                ("Long-term diet", "Single food query", "Long-term DNAm contrast", "Whole-diet exposure; individual-food effect unresolved")]
    for i, (name, query, experiment, limitation) in enumerate(examples):
        y = .83 - i * .29
        ax.text(0, y, name, fontweight="bold", color=COLORS["food"], transform=ax.transAxes)
        ax.text(.2, y, query + "  →  " + experiment, transform=ax.transAxes)
        ax.text(.2, y - .1, limitation, color=COLORS["neutral"], transform=ax.transAxes, fontsize=8)
    save(fig, 3, ["case", "query", "study_condition", "applicability_limit"], examples, "Figure 3. Intended evidence-navigation cases. Serving quantities describe queries and experimental exposures; no molecular response is scaled by their ratio and no clinical improvement is inferred.")
    # Figure 4: only measured latency is plotted; scientific evaluation remains pending.
    fig, ax = plt.subplots(figsize=(7.2, 3.3)); rows = []
    if metrics:
        for i, row in enumerate(metrics["results"]):
            value = row["metrics"]["p95_s"]
            ax.scatter(i, value, color=COLORS["neutral"], s=22)
            rows.append((row["id"], row["metrics"]["n"], value, row["target_s"]))
        ax.axhline(1, color="#9da8ae", linestyle="--", linewidth=1)
        ax.set(xlabel="Frozen query case (ordered as benchmark manifest)", ylabel="Warm p95 (s)")
        ax.set_xticks(range(len(rows)), [str(i + 1) for i in range(len(rows))], fontsize=7)
        ax.set_title("Technical latency; scientific validation is evaluated separately", loc="left", fontsize=10)
    else:
        ax.axis("off"); ax.text(.05, .65, "Fresh benchmark: pending", transform=ax.transAxes, fontsize=12)
        rows.append(("fresh_benchmark", 0, "pending", "not_measured"))
    ax.text(.05, -.3 if metrics else .35, f"Human-approved scientific facts: {reviews['expert_verified']} / {reviews['fact_candidates']} (pending)\nSealed-query scientific accuracy: not yet evaluated", transform=ax.transAxes, fontsize=8, color=COLORS["neutral"])
    save(fig, 4, ["case", "warm_requests", "p95_seconds", "target_seconds"], rows, "Figure 4. Technical and scientific validation states. When present, latency uses 100 warm requests per case with first requests excluded. Engineering repeats are not independent biological samples. Expert review and sealed-query accuracy are not inferred from automated tests.")
    return packages


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--metadata", type=Path, required=True)
    p.add_argument("--review-manifest", type=Path, required=True)
    p.add_argument("--performance", type=Path)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(render(a.metadata, a.review_manifest, a.performance, a.output)))
