"""Deterministic synthetic corpus for the seekdb full-text index benchmark."""

import random

COMMON = """
the of and to in a is that for it as was with be by on not he i this are or his from
at which but have an they one you had we all her she there their been if more when
will would who so no said about out up its into than them can only other new some
could time these two may then do first any my now such like our over man me even
most made after also did many before must through back years where much your way
well down should because each just those people how too little state good very make
world still own see men work long get here between both life being under never day
same another know while last might us great old year off come since against go
came right used take three
""".split()

DOMAIN = """
database index query fulltext search token document keyword tokenizer parser posting
list performance benchmark oceanbase seekdb vector memory storage table column primary
transaction cluster optimizer cost plan page block tablet partition shard replica log
redo undo checkpoint compression encode decode latency throughput concurrent lock latch
row scan sort join aggregate hash nested loop merge cardinality selectivity estimate
""".split()

STOPWORDS = set(COMMON[:60])


def build_vocab():
    """Ranked vocabulary: head words are far more frequent than tail words."""
    vocab = list(dict.fromkeys(DOMAIN + COMMON))
    weights = []
    for rank in range(len(vocab)):
        weights.append(1.0 / ((rank + 1) ** 1.05))
    total = sum(weights)
    cumulative = []
    running = 0.0
    for w in weights:
        running += w / total
        cumulative.append(running)
    cumulative[-1] = 1.0
    return vocab, cumulative


VOCAB, CUMULATIVE = build_vocab()


def _pick(rnd):
    import bisect
    return VOCAB[bisect.bisect_left(CUMULATIVE, rnd.random())]


def gen_document(rnd, min_body=60, max_body=220):
    title_len = rnd.randint(5, 12)
    title = " ".join(_pick(rnd) for _ in range(title_len))
    body_len = rnd.randint(min_body, max_body)
    body = " ".join(_pick(rnd) for _ in range(body_len))
    return title, body


def gen_rows(rows, seed=20261031):
    rnd = random.Random(seed)
    for i in range(1, rows + 1):
        title, body = gen_document(rnd)
        yield i, title, body


def gen_queries(count, seed=777):
    """Mixed workload: single-term natural-language hits plus boolean combos."""
    rnd = random.Random(seed)
    queries = []
    for _ in range(count):
        kind = rnd.random()
        if kind < 0.45:
            term = _pick(rnd)
            queries.append(("nl", "select id from bench_main where match(body) "
                                 "against('%s')" % term))
        elif kind < 0.75:
            a, b = _pick(rnd), _pick(rnd)
            queries.append(("bool2", "select id from bench_main where match(body) "
                                      "against('+%s +%s' in boolean mode)" % (a, b)))
        elif kind < 0.9:
            a, b = _pick(rnd), _pick(rnd)
            queries.append(("boolminus", "select id from bench_main where match(body) "
                                         "against('+%s -%s' in boolean mode)" % (a, b)))
        else:
            term = _pick(rnd)
            queries.append(("nl2", "select id from bench_main where match(body) "
                                   "against('%s')" % term))
    return queries
