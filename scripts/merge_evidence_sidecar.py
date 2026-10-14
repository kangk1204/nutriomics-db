"""Add validated source rows without replacing operational foods, effects or meals."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import migrate

ORDER=("ng_release","ng_entity","ng_study","ng_mapping","ng_dataset","ng_evidence")


def merge(target:Path, source:Path, source_archive:Path|None=None)->dict:
    if target.resolve()==source.resolve():
        raise ValueError("Source must remain separate from the operational database")
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    src=sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro",uri=True)
    src.row_factory=sqlite3.Row
    if src.execute("pragma foreign_key_check").fetchall():
        raise ValueError("Source has invalid references")
    dst=sqlite3.connect(target,timeout=60)
    dst.row_factory=sqlite3.Row
    dst.execute("pragma foreign_keys=on")
    migrate(dst)
    report={"source":str(source),"source_sha256":digest,"tables":{}}
    try:
        dst.execute("begin immediate")
        for table in ORDER:
            n=0
            for source_row in src.execute(f"select * from {table}"):
                row=dict(source_row)
                if table=="ng_release" and source_archive:
                    archived=source_archive/(row["sha256"]+"_"+Path(row["local_path"].replace("\\","/")).name)
                    if not archived.is_file() or hashlib.sha256(archived.read_bytes()).hexdigest()!=row["sha256"]:
                        raise ValueError(f"Missing or changed source snapshot: {row['id']}")
                    row["local_path"]=str(archived)
                old=dst.execute(f"select * from {table} where id=?",(row["id"],)).fetchone()
                if old:
                    old=dict(old)
                    if table=="ng_entity":
                        identity=("kind","namespace","identifier")
                        if any(old[k]!=row[k] for k in identity):
                            raise ValueError(f"Entity identity conflict: {row['id']}")
                        # Source-specific descriptions are retained without silently changing identity.
                        metadata=json.loads(old["metadata_json"] or "{}")
                        incoming=json.loads(row["metadata_json"] or "{}")
                        if incoming != metadata:
                            history=metadata.get("source_metadata_history",[])
                            entry={"sidecar_sha256":digest,"label":row["label"],"metadata":incoming}
                            if entry not in history:
                                history.append(entry)
                            metadata["source_metadata_history"]=history
                            dst.execute("update ng_entity set metadata_json=? where id=?",(json.dumps(metadata),row["id"]))
                    elif old!=row:
                        raise ValueError(f"Immutable source row conflict: {table}/{row['id']}")
                    continue
                columns=list(row)
                placeholders=",".join("?" for _ in columns)
                dst.execute(f"insert into {table} ({','.join(columns)}) values ({placeholders})",[row[k] for k in columns])
                n+=1
            report["tables"][table]=n
        if dst.execute("pragma foreign_key_check").fetchall():
            raise ValueError("Merged database has invalid references")
        dst.commit()
    except BaseException:
        dst.rollback()
        raise
    finally:
        src.close()
        dst.close()
    return report


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--target",type=Path,required=True)
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--source-archive",type=Path)
    args=parser.parse_args()
    print(json.dumps(merge(args.target,args.source,args.source_archive),ensure_ascii=False,indent=2))
