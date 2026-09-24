"""Per-client account classifier: TF-IDF features and logistic regression.

A small, fast model trained on the client's confirmed codings and retrained after every
correction (milliseconds at this scale). It is an independent second opinion to the language
model: when the two agree, coding confidence rises; when they disagree, the line is doubtful.
It cannot predict an account it has never seen, which is why it is only one signal.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import FeatureUnion, Pipeline

from accrueboard.retrieval.knowledge import KnowledgeEntry


@dataclass(frozen=True)
class Prediction:
    account: str
    probability: float
    ranked: tuple[tuple[str, float], ...]
    """Top accounts with their probabilities, most likely first."""


def features(vendor: str, description: str) -> str:
    return f"{vendor} {description}"


class AccountClassifier:
    def __init__(self) -> None:
        self._model: Pipeline | None = None
        self._single_class: str | None = None
        self.trained_on = 0

    def fit(self, entries: Sequence[KnowledgeEntry]) -> "AccountClassifier":
        self.trained_on = len(entries)
        labels = [e.account for e in entries]
        classes = sorted(set(labels))
        self._model, self._single_class = None, None
        if len(classes) == 1:
            self._single_class = classes[0]
        elif len(classes) > 1:
            model = Pipeline(
                [
                    (
                        "features",
                        FeatureUnion(
                            [
                                ("words", TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)),
                                (
                                    "chars",
                                    TfidfVectorizer(
                                        analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True
                                    ),
                                ),
                            ]
                        ),
                    ),
                    ("clf", LogisticRegression(max_iter=2000, C=10.0)),
                ]
            )
            model.fit([features(e.vendor_name, e.description) for e in entries], labels)
            self._model = model
        return self

    @property
    def is_trained(self) -> bool:
        return self._model is not None or self._single_class is not None

    def predict(self, vendor: str, description: str, top: int = 3) -> Prediction | None:
        if self._single_class is not None:
            return Prediction(self._single_class, 1.0, ((self._single_class, 1.0),))
        if self._model is None:
            return None
        probabilities = self._model.predict_proba([features(vendor, description)])[0]
        classes = self._model.classes_
        order = np.argsort(-probabilities, kind="stable")[:top]
        ranked = tuple((str(classes[i]), round(float(probabilities[i]), 6)) for i in order)
        return Prediction(ranked[0][0], ranked[0][1], ranked)
