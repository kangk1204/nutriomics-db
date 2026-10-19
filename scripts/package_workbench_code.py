"""Local source archive with hashes; no credentials, data dumps or publication."""

import argparse
import json
import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def portable_r_sources(code):
    """Keep exact producer bytes when making a portable default-path copy."""
    import hashlib
    changes = {}
    previous = Path(code) / 'portable-path-changes.json'
    if previous.is_file():
        for row in json.loads(previous.read_text(encoding='utf-8')):
            paths = [Path(row['file']), Path(row['original_bytes_path'])]
            if any(p.is_absolute() or '..' in p.parts for p in paths):
                raise ValueError('Source archive manifest paths must stay within the code package')
            current, archived = (Path(code) / p for p in paths)
            if (current.is_file() and archived.is_file()
                and hashlib.sha256(current.read_bytes()).hexdigest() == row['portable_sha256']
                and hashlib.sha256(archived.read_bytes()).hexdigest() == row['original_sha256']):
                changes[row['file']] = row
    for p in Path(code).glob('scripts/*.R'):
        original_bytes = p.read_bytes()
        original = original_bytes.decode('utf-8')
        portable = re.sub(r'"/home/[^/]+/projects/[^/]+/expansion/\d{4}-\d{2}-\d{2}"',
            'Sys.getenv("NUTRIOMICS_ANALYSIS_ROOT", unset="expansion")', original)
        if portable == original:
            continue
        relative = Path('source_snapshots/default_path_originals') / p.name
        archived = Path(code) / relative
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_bytes(original_bytes)
        portable_bytes = portable.encode('utf-8')
        p.write_bytes(portable_bytes)
        changes[p.relative_to(code).as_posix()] = {'file': p.relative_to(code).as_posix(),
            'original_sha256': hashlib.sha256(original_bytes).hexdigest(),
            'original_bytes_path': relative.as_posix(),
            'portable_sha256': hashlib.sha256(portable_bytes).hexdigest(),
            'change': 'default root argument only; exact original producer bytes archived separately'}
    rows = [changes[k] for k in sorted(changes)]
    (Path(code) / 'portable-path-changes.json').write_text(json.dumps(rows, indent=2), encoding='utf-8')
    return rows


def package(output):
    import hashlib

    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    code = output / "code"
    code.mkdir()
    files = [
        *ROOT.glob("nutrigenomics/*.py"),
        *ROOT.glob("nutrigenomics/candidate_rules/*.py"),
        *ROOT.glob("scripts/*.R"),
        *ROOT.glob("tests/*.py"),
        *ROOT.glob("tests/*.R"),
        *ROOT.glob("ui/*.py"),
    ]
    scripts = [
        p
        for p in ROOT.glob("scripts/*.py")
        if not any(
            s in p.name
            for s in ("deploy", "tailscale", "stage_", "restart_", "completion", "secondary_")
        )
    ]
    # Server operators and transport helpers are project-specific, not public
    # dependencies of the portable acquisition/build APIs.
    scripts = [
        p
        for p in scripts
        if p.name == 'package_workbench_code.py' or (
            "scripts.tailscale_project" not in p.read_text(encoding="utf-8")
            and not re.search(r"/home/[^/]+/projects/nutrigenomics-db", p.read_text(encoding="utf-8")))
    ]
    files += scripts
    files += list(ROOT.glob('source_snapshots/default_path_originals/*.R'))
    files += list(ROOT.glob('source_snapshots/candidate_layers/*.py'))
    files += list(ROOT.glob('source_snapshots/clinical_layers/*.py'))
    files += list(ROOT.glob('source_snapshots/performance_layers/*.py'))
    files += [ROOT/'portable-path-changes.json']
    files += [ROOT/'scripts/setup_rebuild_runtime.sh']
    files += list(ROOT.glob('config/rebuild-runtime-*.lock.txt'))
    files += [*ROOT.glob("frontend/explorer/src/*"), *ROOT.glob("frontend/explorer/scripts/*")]
    files += [
        ROOT / "frontend/explorer" / f
        for f in (
            "package.json",
            "pnpm-lock.yaml",
            "pnpm-workspace.yaml",
            "vite.config.ts",
            "tsconfig.json",
            "index.html",
            "README.md",
        )
    ]
    files += [
        ROOT / f
        for f in ("pyproject.toml", "uv.lock", "environment-python-windows.lock.txt", "environment-r.yml", "README.md", "LICENSE", "LICENSE-DATA.md", "CITATION.cff", ".gitignore", ".gitattributes")
    ]
    files += [
        ROOT / "config" / f
        for f in (
            "human_exposures.v1.json",
            "explorer_evidence_curations.json",
            "explorer_export_rights.json",
            "explorer_scope.json",
            "update_sources.json",
            "publication_rights_20261014.json",
            "kfind_food_selection.json",
            "study_groups.json",
            "foodb_food_crosswalk.template.json",
            "literature_reacquisition_pmids.json",
            "literature_candidate_entities.json",
            "clinical_endpoints.v1.json",
            "clinical_source_downloads.v1.json",
        )
    ]
    files += list(ROOT.glob("docs/WORKBENCH*"))
    files += [ROOT/'docs/MDPI_DATA_SUBMISSION_PREPARATION_ko.md']
    files += [ROOT/'docs/PUBLICATION_RUNBOOK_ko.md']
    files += [ROOT/'docs/PUBLIC_DATA_DICTIONARY.md']
    files += [ROOT/'docs/FINAL_UPGRADE_REPORTING_ko.md']
    files += [ROOT/'docs/ANALYSIS_INPUT_AUDIT_20261019.md']
    files += [ROOT/'docs/EXPLORER_PERFORMANCE_ko.md']
    files += list(ROOT.glob('docs/CLINICAL_ENDPOINTS*'))
    files += list(ROOT.glob('docs/CLINICAL_ENDPOINT_SOURCE*'))
    files += [ROOT/'docs/PUBLIC_README.md']
    files += list(ROOT.glob('deploy/public/*'))
    files += list(ROOT.glob('.github/workflows/*.yml'))
    available_scripts = {p.stem for p in scripts}
    excluded_tests = []
    import ast
    for path in list(files):
        if path.parent != ROOT / "tests" or path.suffix != ".py":
            continue
        dependencies = []
        if path.name == "test_primary_affinity_record.py":
            files.remove(path)
            excluded_tests.append({"path": str(path.relative_to(ROOT)), "reason": "private DTI expansion tools are outside the public NutriOmics data package"})
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("scripts."):
                dependencies.append(node.module.split('.')[1])
            elif isinstance(node, ast.ImportFrom) and node.module == "scripts":
                dependencies.extend(n.name for n in node.names)
            elif isinstance(node, ast.Import):
                dependencies.extend(n.name.split('.')[1] for n in node.names if n.name.startswith('scripts.'))
        absent = sorted(set(dependencies) - available_scripts)
        if absent:
            files.remove(path)
            excluded_tests.append({"path": str(path.relative_to(ROOT)), "reason": "depends on project-specific server tooling", "modules": absent})
    for p in files:
        if not p.is_file():
            continue
        relative = p.relative_to(ROOT)
        target = code / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, target)
    portable_r_sources(code)
    for p in [code/'README.md', *code.glob('docs/*.md')]:
        if p.is_file():
            text=re.sub(r'/home/[^/]+/projects/nutrigenomics-db', '<project-root>', p.read_text(encoding='utf-8'))
            text=re.sub(r'[\w.-]+@100\.\d+\.\d+\.\d+', '<research-server>', text)
            text=re.sub(r'100\.\d+\.\d+\.\d+', '<research-server-address>', text)
            p.write_text(text,encoding='utf-8')
    for p in code.glob('config/*.json'):
        text=p.read_text(encoding='utf-8')
        text=re.sub(r'/home/[^/]+/projects/nutrigenomics-db', '<project-root>', text)
        p.write_text(text,encoding='utf-8')
    for p in code.glob('tests/*.py'):
        text=p.read_text(encoding='utf-8')
        text=re.sub(r'/home/[^/]+/projects/nutrigenomics-db', '/home/research/test-project', text)
        p.write_text(text,encoding='utf-8')
    if (code/'docs/PUBLIC_README.md').is_file():
        text=(code/'docs/PUBLIC_README.md').read_text(encoding='utf-8').replace('(PUBLICATION_RUNBOOK_ko.md)','(docs/PUBLICATION_RUNBOOK_ko.md)').replace('(WORKBENCH_PUBLIC_REBUILD_ko.md)','(docs/WORKBENCH_PUBLIC_REBUILD_ko.md)').replace('(WORKBENCH_CANDIDATE_LAYERS_ko.md)','(docs/WORKBENCH_CANDIDATE_LAYERS_ko.md)').replace('(../LICENSE)','(LICENSE)').replace('(../LICENSE-DATA.md)','(LICENSE-DATA.md)')
        (code/'README.md').write_text(text,encoding='utf-8')
    (code / "LICENSE-NOTICE.md").write_text(
        "# License and scope\n\nProject-authored software: MIT (see LICENSE). Project-authored curation/analysis outputs: CC BY 4.0 only where affirmative per-source/output policy permits them (see LICENSE-DATA.md). Third-party dependencies retain their own licenses. No dependency binaries, source data, credentials or private server environment files are bundled. This candidate code archive does not imply scientific review, dataset DOI registration or final submission readiness. Build the frontend from its lock before creating the public container. Server-specific tests requiring excluded transport/deployment helpers are listed in excluded-tests.json; portable tests are retained.\n",
        encoding="utf-8",
    )
    (code / 'excluded-tests.json').write_text(json.dumps(excluded_tests, indent=2), encoding='utf-8')
    manifest = {
        str(p.relative_to(code)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(code.rglob("*"))
        if p.is_file()
    }
    (output / "code-files.sha256.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    archive = output / "nutriomics-code-review.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
        for p in sorted(code.rglob("*")):
            if p.is_file():
                handle.write(p, str(p.relative_to(output)).replace("\\", "/"))
        handle.write(output / "code-files.sha256.json", "code-files.sha256.json")
    return {
        "archive": str(archive.resolve()),
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "files": len(manifest),
        "published": False,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    print(json.dumps(package(p.parse_args().output)))
