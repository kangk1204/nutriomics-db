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
    metrics = json.loads(Path(performance).read_text(encoding="utf-8")) if performance else None
    counts = data["counts"]
    reviews.setdefault("fact_candidates", reviews.get("primary_paper_fact_candidates", 0))
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
    fig, ax = plt.subplots(figsize=(7.2, 4.1)); ax.set(xlim=(0, 10), ylim=(0, 4.5)); ax.axis("off")
    blocks = [(0.1, 3.0, "Food composition", "food", "Source food ID, state\nquantity, unit, basis"),
              (3.35, 3.0, "Human intervention", "curation", "Exposure, arm, contrast\nTissue, time, sample n"),
              (6.6, 3.0, "Omics results", "omics", "Five-part result key\nEffect, SE, P, FDR"),
              (0.1, 0.3, "Source and review", "neutral", "Hashes, source rights\nHuman review: pending"),
              (3.35, 0.3, "Clinical summaries", "curation", "Reported summaries\nUnits, times, group n"),
              (6.6, 0.3, "Versioned release", "neutral", "SQLite · Parquet · CSV\nmanifest, policy, review")]
    for x, y, title, color, text in blocks:
        ax.add_patch(FancyBboxPatch((x, y), 3, 1.2, boxstyle="round,pad=0.04,rounding_size=.05", facecolor="#f5f7f8", edgecolor=COLORS[color], linewidth=1))
        ax.text(x + .15, y + .94, title, fontweight="bold", color=COLORS[color]); ax.text(x + .15, y + .57, text, va="top", linespacing=1.5)
    for x1, y1, x2, y2 in [(3.15, 3.6, 3.3, 3.6), (6.4, 3.6, 6.55, 3.6),
                           (4.85, 2.9, 4.85, 1.6), (6.4, .9, 6.55, .9),
                           (8.1, 2.9, 8.1, 1.6)]:
        ax.annotate("", (x2, y2), (x1, y1), arrowprops={"arrowstyle": "->", "color": COLORS["neutral"]})
    ax.text(.1, 2.5, "Candidate food/component mapping;\ndose equivalence checked separately", fontsize=7.5)
    ax.text(3.55, 2.1, "Shared study context\nnot mediation", fontsize=7.5,
            bbox={"facecolor":"white", "edgecolor":"none", "pad":2})
    ax.text(6.7, 2.1, "Packaged results\nnot personal prediction", fontsize=7.5,
            bbox={"facecolor":"white", "edgecolor":"none", "pad":2})
    save(fig, 1, ["entity", "fields"], [(b[2], b[4].replace('\n', '; ')) for b in blocks], "Figure 1. Candidate data model and release workflow. Clinical summaries and molecular results retain shared-study context; no mediation or causal biological pathway is inferred. Arrows represent data linkage and packaging.")
    # Figure 2: distinct grains; composition relations and omics records are not pooled.
    fig, axes = plt.subplots(1, 3, figsize=(9.6, max(4.8, .49 * len(counts["science_kinds"]) + .8)), gridspec_kw={"wspace": 1.1})
    sets = [list(counts["node_kinds"].items()), list(counts["science_kinds"].items()), list(counts["effect_types"].items())]
    plotted = []
    for panel, ax, entries, title, color in zip("ABC", axes, sets, ["Entities", "Curation records", "Quantitative results"], ["food", "curation", "omics"], strict=True):
        entries = [(k, v) for k, v in entries if k != "curation"]
        labels, values = zip(*entries) if entries else ([], [])
        ax.barh(range(len(values)), values, color=COLORS[color], height=.65)
        span = max(values, default=1)
        ax.set_xlim(0, span * 1.4)
        for i, value in enumerate(values):
            ax.text(value + span * .025, i, f"{value:,}", va="center", fontsize=7)
        def label(value, panel=panel):
            if panel == "C" and value == "dmr":
                return "region tests\n(native dmr)"
            if panel == "A" and value == "gene":
                return "gene\nidentifier"
            return value.replace('_', '\n')
        ax.set_yticks(range(len(values)), [label(x) for x in labels], fontsize=7)
        ax.invert_yaxis(); ax.set_title(panel + "  " + title, loc="left", fontsize=10)
        ax.ticklabel_format(axis="x", style="sci", scilimits=(0, 4)); ax.set_xlabel("Records", fontsize=8)
        plotted.extend((panel, k, v) for k, v in entries)
    save(fig, 2, ["panel", "type", "records"], plotted, "Figure 2. Actual public-candidate counts by data grain. The administrative singleton curation record is omitted from panel B. Entity namespaces are not unique biological genes; quantitative records are feature × contrast × analysis-version results, not participants. Native feature type dmr is retained in source data; fixed-window regional tests are not discovered or validated DMRs. Rights-restricted source values are excluded; predictions remain separately labelled.")
    # Figure 3: explicitly stated use cases, supported by the requested case design.
    fig, ax = plt.subplots(figsize=(7.2, 3.5)); ax.axis("off")
    examples = [("Yogurt", "Input: 150 g", "Acute study: 800 g", "Dose mismatch; product identity unverified"),
                ("Fatty-acid meal", "Food / component query", "PUFA vs SFA, 6 h", "Compare study changes; retain PBMC context"),
                ("Long-term diet", "Single food query", "Long-term DNAm contrast", "Whole-diet exposure; individual-food effect unresolved")]
    for i, (name, query, experiment, limitation) in enumerate(examples):
        y = .83 - i * .29
        ax.text(0, y, name, fontweight="bold", color=COLORS["food"], transform=ax.transAxes)
        ax.text(.26, y, query + "  →  " + experiment, transform=ax.transAxes)
        ax.text(.26, y - .1, limitation, color=COLORS["neutral"], transform=ax.transAxes, fontsize=8)
    save(fig, 3, ["case", "query", "study_condition", "applicability_limit"], examples, "Figure 3. Intended evidence-navigation cases. Serving quantities describe queries and experimental exposures; no molecular response is scaled by their ratio and no clinical improvement is inferred.")
    # Figure 4: only measured latency is plotted; scientific evaluation remains pending.
    fig, ax = plt.subplots(figsize=(7.2, 3.3)); rows = []
    if metrics:
        for i, row in enumerate(metrics["results"]):
            value = row["metrics"]["p95_s"]
            complete = row["metrics"]["n"] == metrics["runs_per_case"] and row["metrics"]["errors"] == 0
            ax.scatter(i, value, color=COLORS["neutral"] if complete else "#a54e40", marker="o" if complete else "x", s=22)
            rows.append((row["id"], row["metrics"]["n"], value, row["target_s"], row["metrics"]["errors"], complete))
        for target in sorted({row["target_s"] for row in metrics["results"]}):
            ax.axhline(target, color="#9da8ae", linestyle="--", linewidth=.7)
            ax.text(1.01, target, f"{target:g} s target", transform=ax.get_yaxis_transform(), fontsize=7, va="center")
        if all(row[2] > 0 for row in rows):
            ax.set_yscale("log")
        ax.set(xlabel="Frozen query case (ordered as benchmark manifest)", ylabel="Warm p95 (s)")
        ticks = sorted({0, len(rows)-1, *range(0, len(rows), max(1, len(rows)//8))})
        ax.set_xticks(ticks, [str(i + 1) for i in ticks], fontsize=7)
        ax.set_title("Technical latency; scientific validation is evaluated separately", loc="left", fontsize=10)
    else:
        ax.axis("off"); ax.text(.05, .65, "Fresh benchmark: pending", transform=ax.transAxes, fontsize=12)
        rows.append(("fresh_benchmark", 0, "pending", "not_measured", "not_measured", False))
    ax.text(.05, -.3 if metrics else .35, f"Human-approved core review records: {reviews['expert_verified']} / {reviews.get('records', reviews.get('expected_records', 'not reported'))} (pending)\nSource-fact candidates: {reviews['fact_candidates']} (not gold labels); sealed-query accuracy: not evaluated", transform=ax.transAxes, fontsize=8, color=COLORS["neutral"])
    save(fig, 4, ["case", "measured_requests", "p95_seconds", "target_seconds", "errors", "complete_error_free_series"], rows, "Figure 4. Technical and scientific validation states. The source table retains the actual number of measured requests and errors for each frozen case. Completed warm series exclude first requests; an aborted preparation is marked as an incomplete series, not a warm measurement. Crosses mark incomplete or error-containing series and do not establish a passing benchmark. HTTP cache hits, startup and operating-system cache preparation must be interpreted with the raw receipt. Engineering repeats are not independent biological samples. Expert review and sealed-query accuracy are not inferred from automated tests.")
    # Figure 5: graph layers are counted separately from molecular result rows.
    entries = list(counts.get("by_tier", {}).items())
    labels = {"observed_composition": "Food composition", "observed_intervention": "Study/component context", "observed": "Published clinical summaries", "predicted": "Cell-line regulatory predictions", "regulatory_identity": "Identifier cross-references", "candidate": "Candidate correspondences", "curated": "Source-curated associations", "extracted": "Automatic text candidates"}
    fig, ax = plt.subplots(figsize=(7.2, max(3.4, len(entries) * .44 + 1.0)))
    values = [value for _, value in entries]
    ax.barh(range(len(entries)), values, color=COLORS["neutral"], height=.65)
    ax.set_yticks(range(len(entries)), [labels.get(key, key) for key, _ in entries], fontsize=8)
    ax.invert_yaxis()
    ax.set_xscale("log")
    ax.set_xlabel("Source relationship records (log scale)")
    ax.set_xlim(.8, max(values, default=1) * 4)
    for i, value in enumerate(values):
        ax.text(value * 1.08, i, f"{value:,}", va="center", fontsize=8)
    ax.set_title("Public graph inventory by evidence layer", loc="left", fontsize=10)
    save(fig, 5, ["layer", "display_label", "relationship_records"], [(key, labels.get(key, key), value) for key, value in entries], "Figure 5. Public graph relationship inventory by source layer. Composition, clinical summaries, study context, identifier correspondences and cell-line predictions have different meanings. Counts do not establish causality, clinical benefit or independent biological replication. Human review remains pending.")
    return packages


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--metadata", type=Path, required=True)
    p.add_argument("--review-manifest", type=Path, required=True)
    p.add_argument("--performance", type=Path)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(render(a.metadata, a.review_manifest, a.performance, a.output)))
