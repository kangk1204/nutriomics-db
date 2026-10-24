"""Document bag logistic baseline and latent maximum-witness linear MIL."""

import numpy as np

from .features_v5 import SEED, pool_features


class ConstantBinary:
    classes_ = np.array([0, 1])

    def __init__(self, value):
        self.value = float(value)

    def predict_proba(self, X):
        return np.array([[1 - self.value, self.value] for _ in X])


def binary_fit(features, labels, C=1.0, weights=None):
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline

    if len(set(labels)) < 2:
        return ConstantBinary(labels[0] if labels else 0)
    pipeline = make_pipeline(
        DictVectorizer(),
        LogisticRegression(C=C, max_iter=500, class_weight="balanced", solver="liblinear", random_state=SEED),
    )
    pipeline.fit(features, labels, logisticregression__sample_weight=weights)
    return pipeline


class BagClassifier:
    """Positive document bags choose one latent witness, never all sentences."""

    def __init__(self, mode="mil", C=1.0, iterations=4):
        if mode not in {"logistic", "mil"}:
            raise ValueError("Unknown bag learner")
        self.mode, self.C, self.iterations = mode, C, iterations
        self.models, self.selection_history = {}, {}

    def fit(self, bags, targets, labels):
        self.labels = list(labels)
        pooled = [pool_features(b) for b in bags]
        for label in self.labels:
            y = [int(label in t) for t in targets]
            model = binary_fit(pooled, y, self.C)
            history = []
            if self.mode == "mil":
                for _ in range(self.iterations):
                    rows, truth, weights, selected = [], [], [], []
                    for bag, positive in zip(bags, y):
                        if not bag:
                            continue
                        if positive:
                            scores = model.predict_proba(bag)[:, 1]
                            witness = int(np.argmax(scores))
                            selected.append(witness)
                            rows.append(bag[witness])
                            truth.append(1)
                            weights.append(1.0)
                        else:
                            rows.extend(bag)
                            truth.extend([0] * len(bag))
                            weights.extend([1 / len(bag)] * len(bag))
                    model = binary_fit(rows, truth, self.C, weights)
                    history.append(selected)
                    if len(history) > 1 and history[-1] == history[-2]:
                        break
            self.models[label] = model
            self.selection_history[label] = history
        return self

    def predict(self, bags):
        output = []
        for bag in bags:
            item = {}
            for label, model in self.models.items():
                witness_scores = model.predict_proba(bag)[:, 1] if bag else np.array([])
                witness = int(np.argmax(witness_scores)) if len(witness_scores) else None
                score = (
                    (float(max(witness_scores)) if len(witness_scores) else 0.0)
                    if self.mode == "mil"
                    else float(model.predict_proba([pool_features(bag)])[0, 1])
                )
                item[label] = {"score": score, "witness": witness}
            output.append(item)
        return output
