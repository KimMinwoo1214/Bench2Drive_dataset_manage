"""The bbox consensus must not depend on the order frames were read in.

Splitting the scan across workers is only safe because the consensus is a
reduction. That holds for the counting, but picking a winner out of the counts
is where order can leak back in: Counter.most_common returns whichever entry
was inserted first when two are tied, and with a worker pool that is whoever
happened to finish first. These tests pin the tie-break to the value itself.
"""

from __future__ import annotations

import unittest
from collections import Counter

from fix_tl_bbox_permutation import resolve_consensus


class ResolveConsensusTest(unittest.TestCase):
    def test_majority_wins(self) -> None:
        votes = {("k",): Counter({(1.0, 2.0): 7, (3.0, 4.0): 2})}
        consensus, disputed = resolve_consensus(votes)
        self.assertEqual(consensus[("k",)], (1.0, 2.0))
        # Not unanimous, so the key is recorded as disputed.
        self.assertEqual(disputed, 1)

    def test_unanimous_is_not_disputed(self) -> None:
        votes = {("k",): Counter({(1.0, 2.0): 9})}
        consensus, disputed = resolve_consensus(votes)
        self.assertEqual(consensus[("k",)], (1.0, 2.0))
        self.assertEqual(disputed, 0)

    def test_tie_does_not_depend_on_insertion_order(self) -> None:
        first = Counter()
        first[(3.0, 4.0)] = 5
        first[(1.0, 2.0)] = 5
        second = Counter()
        second[(1.0, 2.0)] = 5
        second[(3.0, 4.0)] = 5
        self.assertEqual(
            resolve_consensus({("k",): first})[0],
            resolve_consensus({("k",): second})[0],
        )

    def test_merging_partial_counts_matches_one_pass(self) -> None:
        # What a worker pool produces: the same votes, arrived at in shards.
        whole = Counter({(1.0, 2.0): 4, (3.0, 4.0): 6})
        shards = [Counter({(1.0, 2.0): 3}), Counter({(3.0, 4.0): 6}),
                  Counter({(1.0, 2.0): 1})]
        merged = Counter()
        for shard in shards:
            merged.update(shard)
        self.assertEqual(
            resolve_consensus({("k",): merged}),
            resolve_consensus({("k",): whole}),
        )


if __name__ == "__main__":
    unittest.main()
