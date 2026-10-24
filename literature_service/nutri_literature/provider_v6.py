"""Share fixed NER/backbone objects; never load chemical or BERT weights twice."""

from pathlib import Path

from .candidates_v5 import UnionCandidates
from .candidates_v4 import directory_hash


class SharedCandidates(UnionCandidates):
    def __init__(self, home, encoder, provider="union"):
        super().__init__(home, provider)
        self.encoder = encoder

    def load(self):
        if self.manifest.get("available"):
            return
        import spacy
        import en_ner_bc5cdr_md

        self.encoder.load()
        chemical = spacy.load("en_ner_bc5cdr_md", disable=["tagger", "parser", "lemmatizer"])
        chemical_sha = directory_hash(Path(en_ner_bc5cdr_md.__file__).parent)
        for member in self.providers:
            member.chemical = chemical
            if member.gene_provider == "biomedbert":
                if self.encoder.ner_model.config.id2label != {0: "O", 1: "B-GENE", 2: "I-GENE"}:
                    raise ValueError("Pinned frozen gene head labels differ")
                member.gene = self.encoder.ner_model
                member.tokenizer = self.encoder.tokenizer
                sha = self.encoder.manifest["model_sha256"]
            else:
                import en_ner_bionlp13cg_md

                member.gene = spacy.load("en_ner_bionlp13cg_md", disable=["tagger", "parser", "lemmatizer"])
                sha = directory_hash(Path(en_ner_bionlp13cg_md.__file__).parent)
            member.manifest.update(available=True, chemical_model_sha256=chemical_sha, gene_model_sha256=sha)
        self.manifest.update(
            available=True,
            members=[p.manifest for p in self.providers],
            chemical_model_instances=1,
            biomedbert_backbone_shared_with_encoder=True,
        )
