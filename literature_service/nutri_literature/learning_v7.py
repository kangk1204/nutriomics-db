"""Entity validity, semantic types and directions have independent targets."""
from .contract_v7 import TYPES


class SeparatedHead:
    def fit(self, records, c=1.):
        import numpy as np
        from sklearn.feature_extraction import DictVectorizer
        from sklearn.preprocessing import MaxAbsScaler
        from sklearn.linear_model import LogisticRegression
        self.vectorizer = DictVectorizer(dtype=np.float32)
        matrix = self.vectorizer.fit_transform([semantic_features(r["features"]) for r in records])
        self.scaler = MaxAbsScaler().fit(matrix)
        matrix = self.scaler.transform(matrix)
        self.models = {}
        targets = {"valid": [(i, int(r["targets"]["entity_valid"])) for i, r in enumerate(records)]}
        supervised = [(i, r) for i, r in enumerate(records) if r["targets"]["semantic_supervised"]]
        for label in TYPES:
            targets[label] = [(i, int(label in r["targets"]["semantic_labels"])) for i, r in supervised]
        targets["exists"] = [(i, int(bool(r["targets"]["semantic_labels"]))) for i, r in supervised]
        targets["direction"] = [(i, r["targets"]["direction"]) for i, r in supervised if r["targets"]["direction"]]
        for name, values in targets.items():
            if not values:
                self.models[name] = None
            elif len({v for _, v in values}) == 1:
                self.models[name] = values[0][1]
            else:
                indices, labels = zip(*values)
                self.models[name] = LogisticRegression(C=c, solver="liblinear" if name != "direction" else "lbfgs",
                    max_iter=500, random_state=20261017).fit(matrix[list(indices)], labels)
        return self

    def predict(self, records):
        if not records:
            return []
        matrix = self.scaler.transform(self.vectorizer.transform([semantic_features(r["features"]) for r in records]))
        results = [{} for _ in records]
        for key, model in self.models.items():
            if model is None or isinstance(model, (int, float, str)):
                for row in results:
                    row[key] = {str(model): 1.} if key == "direction" and model is not None else (float(model or 0) if key != "direction" else {})
            else:
                values = model.predict_proba(matrix)
                for row, scores in zip(results, values):
                    row[key] = dict(zip(map(str, model.classes_), map(float, scores))) if key == "direction" else float(scores[list(model.classes_).index(1)])
        return results


def semantic_features(features):
    # ID availability/normalizer abstention must not become a semantic target.
    return {k: v for k, v in features.items() if not any(t in k for t in (":resolved", ":reason:", ":selected"))}


def selected(row, threshold):
    # This score is a train-selected decision function, not a nutrition probability.
    return [label for label in TYPES if row[label] * row["valid"] >= threshold]
