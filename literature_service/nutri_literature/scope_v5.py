"""Independent cue detection and cue-conditioned CPU CRF scopes."""

from .features_v5 import scope_features, tokens_for


def _ranges(tokens, labels, label):
    spans, start = [], None
    for i, value in enumerate(list(labels) + ["O"]):
        if value == label and start is None:
            start = i
        if value != label and start is not None:
            spans.append((start, i))
            start = None
    return spans


def predict(sentences, learned):
    from .nlp_v2 import cpu_inference_lock

    with cpu_inference_lock():
        return _predict(sentences, learned)


def _predict(sentences, learned):
    learned.load()
    model = learned.models["scope"]
    scopes = []
    for sentence in sentences:
        tokens = tokens_for(sentence)
        if not tokens:
            continue
        for kind in ("negation", "speculation"):
            features = [scope_features(tokens, i) for i in range(len(tokens))]
            cue_labels = model[kind + "_cue"].predict_single(features)
            for lo, hi in _ranges(tokens, cue_labels, "C"):
                cue_indices = list(range(lo, hi))
                cue = {"start": tokens[lo]["start"], "end": tokens[hi - 1]["end"]}
                cue["text"] = sentence["text"][
                    cue["start"] - sentence["start"] : cue["end"] - sentence["start"]
                ]
                labels = model[kind + "_scope"].predict_single(
                    [scope_features(tokens, i, cue_indices) for i in range(len(tokens))]
                )
                for a, b in _ranges(tokens, labels, "S"):
                    start, end = tokens[a]["start"], tokens[b - 1]["end"]
                    scopes.append(
                        {
                            "kind": kind,
                            "start": start,
                            "end": end,
                            "text": sentence["text"][start - sentence["start"] : end - sentence["start"]],
                            "cue_spans": [cue],
                            "rule_id": "cpu-crf-cue-scope-v5",
                        }
                    )
    return scopes
