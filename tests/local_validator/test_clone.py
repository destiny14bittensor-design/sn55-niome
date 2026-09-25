from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import warnings

import numpy as np

from tools.local_validator.artifacts import ArtifactBundle
from tools.local_validator.evaluator import evaluate_submission, parse_seeds
from tools.local_validator.ingestion import ingest_submission
from tools.local_validator.stage12 import (
    build_kmer_index,
    check_pam,
    run_stage12,
    stage1,
    stage2,
)
from tools.local_validator.stage3 import experiment_seed, run_stage3
from tools.local_validator.stage4 import run_stage4
from tools.local_validator.stage5 import compute_distribution_fidelity, run_stage5
from tools.local_validator.weights import process_scores_top, simulate_weights
from niome_subnet.genomics.submission_builder import build_submission


def base_contract(**rule_overrides):
    rules = {
        "max_mismatches": 0,
        "base_padding": 500,
        "proximity_gate": False,
        "max_experiments": 100,
        "cas_systems": ["Cas9"],
    }
    rules.update(rule_overrides)
    return {
        "seed": "11,29",
        "active_mutations": ["m1"],
        "rules": rules,
        "cell_type": None,
        "mutation_weights": {"m1": 1.25},
        "mutation_regions": {"m1": "exon1"},
    }


def build_submission_fixture(count: int = 10):
    sequence = list("A" * 700)
    rows = []
    patterns = [
        "ACGTACGTACGTACGTACGT",
        "CGTACGTACGTACGTACGTA",
        "GTACGTACGTACGTACGTAC",
        "TACGTACGTACGTACGTACG",
    ]
    for index in range(count):
        start = 20 + index * 35
        guide = patterns[index % len(patterns)]
        sequence[start : start + 20] = guide
        sequence[start + 20 : start + 23] = "AGG"
        rows.append(
            {
                "experiment_id": f"exp-{index}",
                "guideRNA": guide,
                "target_alignment_start": start,
                "target_alignment_end": start + 20,
                "strand": "+",
                "mutation": "m1",
                "cas_system": "Cas9",
            }
        )
    return "".join(sequence), rows


def direct_valid_fixture(count: int = 10):
    rows = []
    for index in range(count):
        guide = ("ACGT" * 5)[index % 4 :] + ("ACGT" * 5)[: index % 4]
        guide = guide[:20]
        rows.append(
            {
                "experiment": {
                    "experiment_id": f"direct-{index}",
                    "guideRNA": guide,
                    "target_alignment_start": 100 + index * 7,
                    "target_alignment_end": 120 + index * 7,
                    "strand": "+" if index % 2 == 0 else "-",
                    "mutation": "m1",
                    "cas_system": "Cas9",
                },
                "features": {
                    "gc": 0.35 + index * 0.02,
                    "distance_to_mutation": index * 7,
                    "gc_score": 0.7 + index * 0.01,
                    "dist_score": 0.98 - index * 0.03,
                    "consistency": 1.0,
                    "offtarget_factor": 1.0,
                    "mutation_weight": 1.25,
                    "cell_type": None,
                    "cell_type_accessibility": 1.0,
                    "mutation_region": "exon1",
                    "region_energy_offset": 0.03,
                },
                "stage1": {"valid": True},
                "stage2": {
                    "structural_score": 0.6 + index * 0.01,
                    "weighted_score": (0.6 + index * 0.01) * 1.25,
                },
            }
        )
    return rows


class IngestionTests(unittest.TestCase):
    def test_id_dedup_and_cap_are_ordered(self):
        contract = base_contract(max_experiments=2)
        rows = [
            {"experiment_id": ""},
            {"experiment_id": "a"},
            {"experiment_id": "a"},
            {"experiment_id": " b "},
            {"experiment_id": "c"},
        ]
        result = ingest_submission(rows, contract)
        self.assertEqual([row["experiment_id"] for row in result.retained], ["a", " b "])
        self.assertEqual(
            result.dropped,
            [
                {"index": 0, "reason": "invalid_experiment_id"},
                {"index": 2, "reason": "duplicate_experiment_id"},
                {"index": 4, "reason": "max_experiments"},
            ],
        )

    def test_non_list_preserves_validator_failure(self):
        with self.assertRaises(AttributeError):
            ingest_submission({"not": "a list"}, base_contract())

    def test_seed_parser_matches_validator(self):
        self.assertEqual(parse_seeds("1, 2,,3"), [1, 2, 3])
        self.assertEqual(parse_seeds(7), [7])


class Stage12Tests(unittest.TestCase):
    def setUp(self):
        self.sequence, rows = build_submission_fixture(1)
        self.row = rows[0]
        self.contract = base_contract()
        self.reference = {"mutation_map": {"m1": 20}, "gene_region": {"start": 0, "end": 500}}

    def test_valid_cas9_plus_and_wrong_pam_reason(self):
        score, reason = stage1(self.row, self.sequence, self.reference["mutation_map"], self.contract)
        self.assertEqual((score, reason), (1.0, "ok"))
        broken = self.sequence[:40] + "AAA" + self.sequence[43:]
        score, reason = stage1(self.row, broken, self.reference["mutation_map"], self.contract)
        self.assertEqual((score, reason), (0.0, "pam_ok"))

    def test_kmer_builder_preserves_missing_last_start(self):
        self.assertEqual(build_kmer_index("ABCDE", k=3), {"ABC": [0], "BCD": [1]})

    def test_stage12_valid_and_duplicate_design(self):
        duplicate = dict(self.row, experiment_id="different-id")
        valid, invalid, provenance = run_stage12(
            [self.row, duplicate],
            contract=self.contract,
            reference=self.reference,
            chromosome_11=self.sequence,
            cell_types={},
        )
        self.assertEqual(len(valid), 1)
        self.assertEqual(invalid[0]["reason"], "duplicate_experiment")
        self.assertEqual(provenance["kmer_k"], 12)


class Stage345Tests(unittest.TestCase):
    def test_stage3_is_seed_deterministic(self):
        valid = direct_valid_fixture(2)
        first, _ = run_stage3(valid, 123)
        second, _ = run_stage3(valid, 123)
        self.assertEqual(first, second)
        self.assertEqual(
            experiment_seed(123, valid[0]["experiment"]),
            experiment_seed(123, valid[0]["experiment"]),
        )

    def test_stage4_rows_two_through_nine_collapse_to_zero(self):
        valid = direct_valid_fixture(9)
        stage3, _ = run_stage3(valid, 123)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = run_stage4(valid, stage3, seed=123)
        self.assertEqual(result["n_valid_experiments"], 9)
        self.assertEqual(result["consistency_factor"], 0.0)

    def test_stage5_has_no_external_reference(self):
        valid = direct_valid_fixture(10)
        stage3, _ = run_stage3(valid, 123)
        fidelity = compute_distribution_fidelity(valid, stage3, base_contract())
        self.assertGreater(fidelity["distribution_fidelity_score"], 0.0)
        self.assertLessEqual(fidelity["distribution_fidelity_score"], 1.0)


class EndToEndTests(unittest.TestCase):
    def test_live_submission_builder_outputs_stage1_valid_rows(self):
        sequence, _ = build_submission_fixture(12)
        contract = base_contract(max_experiments=10)
        reference = {
            "mutation_map": {"m1": 20},
            "gene_region": {"start": 0, "end": 650},
        }
        rows, diagnostics = build_submission(
            contract=contract,
            reference=reference,
            chromosome_11=sequence,
            cell_types={},
        )
        self.assertEqual(len(rows), 10)
        self.assertEqual(diagnostics["selected_count"], 10)
        valid, invalid, _ = run_stage12(
            rows,
            contract=contract,
            reference=reference,
            chromosome_11=sequence,
            cell_types={},
        )
        self.assertEqual(len(valid), 10)
        self.assertEqual(invalid, [])

    def test_complete_evaluation_is_repeatable(self):
        sequence, submission = build_submission_fixture(10)
        artifacts = ArtifactBundle(
            contract=base_contract(),
            hbb_reference={
                "mutation_map": {"m1": 20},
                "gene_region": {"start": 0, "end": 650},
            },
            chromosome_11=sequence,
            cell_types={},
            manifest={"fixture": True},
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            first = evaluate_submission(submission, artifacts, uid=7)
            second = evaluate_submission(submission, artifacts, uid=7)
        self.assertEqual(first.final_score, second.final_score)
        self.assertEqual(first.breakdown, second.breakdown)
        self.assertEqual(len(first.valid_experiments), 10)
        self.assertEqual(len(first.per_seed), 2)
        json.dumps(first.as_dict(), allow_nan=True)

    def test_artifact_path_loader_hashes_all_inputs(self):
        sequence, submission = build_submission_fixture(1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "contract.json").write_text(json.dumps(base_contract()))
            (root / "reference.json").write_text(
                json.dumps({"mutation_map": {"m1": 20}, "gene_region": {"start": 0, "end": 500}})
            )
            (root / "chr11.fa").write_text(f">chr11\n{sequence}\n")
            (root / "celltypes.json").write_text("{}")
            bundle = ArtifactBundle.from_paths(
                contract_path=root / "contract.json",
                hbb_reference_path=root / "reference.json",
                chromosome_11_path=root / "chr11.fa",
                cell_types_path=root / "celltypes.json",
            )
            self.assertEqual(bundle.chromosome_11, sequence)
            self.assertEqual(set(bundle.manifest["files"]), {
                "contract", "hbb_reference", "chromosome_11", "cell_types"
            })


class WeightTests(unittest.TestCase):
    def test_top_distribution_and_owner_mix(self):
        scores = np.asarray([0.0, 10.0, 9.0, 8.0], dtype=np.float32)
        ranked = process_scores_top(scores)
        np.testing.assert_allclose(ranked, [0.0, 0.3 / 0.7, 0.2 / 0.7, 0.2 / 0.7])
        result = simulate_weights(scores.tolist(), owner_uid=0)
        self.assertAlmostEqual(sum(result["final_float_weights"]), 1.0, places=6)
        self.assertAlmostEqual(result["final_float_weights"][0], 0.02, places=6)

    def test_all_zero_falls_back_to_owner(self):
        result = simulate_weights([0.0, 0.0, 0.0], owner_uid=1)
        self.assertEqual(result["emit_uids"], [1])
        self.assertEqual(result["emit_uint16_weights"], [65_535])


class PublicSourceParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from niome_subnet.genomics.validation import stage12 as original12
            from niome_subnet.genomics.validation import stage3 as original3
            from niome_subnet.genomics.validation import stage4 as original4
            from niome_subnet.genomics.validation import stage5 as original5
            from niome_subnet.utils.weight_utils import process_scores_top as original_top
        except ImportError as error:
            raise unittest.SkipTest(f"project runtime dependencies unavailable: {error}")
        cls.original12 = original12
        cls.original3 = original3
        cls.original4 = original4
        cls.original5 = original5
        cls.original_top = staticmethod(original_top)

    def test_stage1_stage2_and_stage3_match_public_source(self):
        sequence, rows = build_submission_fixture(1)
        row, contract = rows[0], base_contract()
        mutation_map = {"m1": 20}
        self.assertEqual(
            stage1(row, sequence, mutation_map, contract),
            self.original12.stage1(row, sequence, mutation_map, contract),
        )
        index = build_kmer_index(sequence, 12)
        local_score, local_info = stage2({}, row, mutation_map, contract, index)
        source_score, source_info = self.original12.stage2(
            {}, row, sequence, mutation_map, contract, index
        )
        self.assertEqual(local_score, source_score)
        self.assertEqual(local_info, source_info)

        valid = direct_valid_fixture(3)
        local_results, local_summary = run_stage3(valid, 77)
        source_results = [self.original3.simulate(item, 77) for item in valid]
        self.assertEqual(local_results, source_results)
        self.assertEqual(local_summary["outcomes"], dict(
            __import__("collections").Counter(item["outcome"] for item in source_results)
        ))

    def test_stage4_stage5_match_public_source(self):
        valid = direct_valid_fixture(10)
        stage3_results, _ = run_stage3(valid, 77)
        local4 = run_stage4(valid, stage3_results, seed=77)
        contract = base_contract()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid_path = root / "valid.json"
            stage3_path = root / "stage3.json"
            reward_path = root / "reward.json"
            contract_path = root / "contract.json"
            distribution_path = root / "distribution.json"
            valid_path.write_text(json.dumps(valid))
            stage3_path.write_text(json.dumps(stage3_results))
            contract_path.write_text(json.dumps(contract))

            old4 = (
                self.original4.VALID_EXPERIMENTS_PATH,
                self.original4.STAGE3_DATASET,
                self.original4.FINAL_REWARD_PATH,
            )
            old5 = (
                self.original5.VALID_EXPERIMENTS_PATH,
                self.original5.STAGE3_DATASET,
                self.original5.CONTRACT_PATH,
                self.original5.FINAL_REWARD_PATH,
                self.original5.DISTRIBUTION_FIDELITY_PATH,
            )
            try:
                self.original4.VALID_EXPERIMENTS_PATH = str(valid_path)
                self.original4.STAGE3_DATASET = str(stage3_path)
                self.original4.FINAL_REWARD_PATH = str(reward_path)
                source4 = self.original4.run_stage4(seed=77)
                for key in (
                    "n_valid_experiments",
                    "total_weighted_score",
                    "consistency_score",
                    "consistency_factor",
                    "final_reward",
                    "model_results",
                ):
                    self.assertEqual(local4[key], source4[key])

                self.original5.VALID_EXPERIMENTS_PATH = str(valid_path)
                self.original5.STAGE3_DATASET = str(stage3_path)
                self.original5.CONTRACT_PATH = str(contract_path)
                self.original5.FINAL_REWARD_PATH = str(reward_path)
                self.original5.DISTRIBUTION_FIDELITY_PATH = str(distribution_path)
                source5 = self.original5.run_stage5()
                local5 = run_stage5(valid, stage3_results, local4, contract)
                for key, value in source5.items():
                    self.assertEqual(local5[key], value)
            finally:
                (
                    self.original4.VALID_EXPERIMENTS_PATH,
                    self.original4.STAGE3_DATASET,
                    self.original4.FINAL_REWARD_PATH,
                ) = old4
                (
                    self.original5.VALID_EXPERIMENTS_PATH,
                    self.original5.STAGE3_DATASET,
                    self.original5.CONTRACT_PATH,
                    self.original5.FINAL_REWARD_PATH,
                    self.original5.DISTRIBUTION_FIDELITY_PATH,
                ) = old5

    def test_top_weight_transform_matches_public_source(self):
        scores = np.asarray([0.0, 1.0, 9.0, 3.0, 2.0], dtype=np.float32)
        np.testing.assert_array_equal(process_scores_top(scores), self.original_top(scores))


if __name__ == "__main__":
    unittest.main()
