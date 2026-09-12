"""Pure-Python evaluation metrics (run anywhere). Use sacrebleu/COMET on the server for official numbers."""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Sequence

CEFR_ORDER = ["A1", "A2", "B1", "B2", "C1"]


def _ngrams(seq: Sequence, n: int) -> Counter:
    return Counter(tuple(seq[i:i + n]) for i in range(len(seq) - n + 1))


def _prf_orders(hyp_units: Sequence, ref_units: Sequence, max_order: int) -> list[tuple[float, float]]:
    out = []
    for n in range(1, max_order + 1):
        h, r = _ngrams(hyp_units, n), _ngrams(ref_units, n)
        th, tr = sum(h.values()), sum(r.values())
        if th == 0 or tr == 0:
            continue
        m = sum((h & r).values())
        out.append((m / th, m / tr))
    return out


def chrf(hyp: str, ref: str, char_order: int = 6, word_order: int = 2, beta: float = 2.0) -> float:
    """chrF++ (0-100): character n-grams up to `char_order` (whitespace removed) + word n-grams up to `word_order`."""
    hyp, ref = (hyp or "").strip(), (ref or "").strip()
    if not hyp or not ref:
        return 0.0 if hyp != ref else 100.0
    orders = _prf_orders(hyp.replace(" ", ""), ref.replace(" ", ""), char_order)
    orders += _prf_orders(hyp.split(), ref.split(), word_order)
    if not orders:
        return 0.0
    p = sum(o[0] for o in orders) / len(orders)
    r = sum(o[1] for o in orders) / len(orders)
    if p == 0 and r == 0:
        return 0.0
    b2 = beta * beta
    return 100.0 * (1 + b2) * p * r / (b2 * p + r)


def corpus_chrf(hyps: Iterable[str], refs: Iterable[str]) -> float:
    pairs = list(zip(hyps, refs))
    return sum(chrf(h, r) for h, r in pairs) / len(pairs) if pairs else 0.0


def alignment_accuracy(pred_counts: Iterable[int | None], ref_counts: Iterable[int]) -> float:
    pairs = list(zip(pred_counts, ref_counts))
    return sum(1 for p, r in pairs if p == r) / len(pairs) if pairs else 0.0


_ARTICLE_RE = re.compile(r"^(der|die|das)\s+", re.IGNORECASE)


def norm_lemma(lemma: str) -> str:
    s = _ARTICLE_RE.sub("", (lemma or "").strip().lower())
    s = s.split(",")[0].strip()
    return s.replace("|", "")


def gloss_prf(pred_lemmas: Iterable[str], ref_lemmas: Iterable[str]) -> tuple[float, float, float]:
    p = {norm_lemma(x) for x in pred_lemmas if x}
    r = {norm_lemma(x) for x in ref_lemmas if x}
    if not p and not r:
        return 1.0, 1.0, 1.0
    tp = len(p & r)
    prec = tp / len(p) if p else 0.0
    rec = tp / len(r) if r else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return prec, rec, f1


def cefr_accuracy(preds: Iterable[str | None], refs: Iterable[str]) -> tuple[float, float]:
    """(exact accuracy, adjacent accuracy: within one level)."""
    pairs = [(p, r) for p, r in zip(preds, refs) if r in CEFR_ORDER]
    if not pairs:
        return 0.0, 0.0
    exact = sum(1 for p, r in pairs if p == r)
    adjacent = sum(1 for p, r in pairs if p in CEFR_ORDER and abs(CEFR_ORDER.index(p) - CEFR_ORDER.index(r)) <= 1)
    return exact / len(pairs), adjacent / len(pairs)
