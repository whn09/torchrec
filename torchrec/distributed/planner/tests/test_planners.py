#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

import unittest
from typing import cast, List, Optional

import torch
from torch import nn
from torchrec import EmbeddingBagCollection, EmbeddingConfig
from torchrec.distributed.embedding import EmbeddingCollectionSharder
from torchrec.distributed.embedding_types import EmbeddingComputeKernel
from torchrec.distributed.embeddingbag import EmbeddingBagCollectionSharder
from torchrec.distributed.planner.enumerators import EmbeddingEnumerator
from torchrec.distributed.planner.perf_models import NoopPerfModel
from torchrec.distributed.planner.planners import EmbeddingShardingPlanner
from torchrec.distributed.planner.proposers import EmbeddingOffloadScaleupProposer
from torchrec.distributed.planner.stats import EmbeddingStats
from torchrec.distributed.planner.storage_reservations import (
    HeuristicalStorageReservation,
)
from torchrec.distributed.planner.types import (
    ParameterConstraints,
    PlannerError,
    PlannerErrorType,
    ShardingOption,
    Topology,
)
from torchrec.distributed.sharding_plan import get_default_sharders
from torchrec.distributed.test_utils.test_model import TestSparseNN
from torchrec.distributed.types import (
    BoundsCheckMode,
    CacheAlgorithm,
    CacheParams,
    DataType,
    EmbeddingModuleShardingPlan,
    KeyValueParams,
    ModuleSharder,
    ShardingPlan,
    ShardingType,
)
from torchrec.modules.embedding_configs import EmbeddingBagConfig


class TWvsRWSharder(EmbeddingBagCollectionSharder, ModuleSharder[nn.Module]):
    def sharding_types(self, compute_device_type: str) -> List[str]:
        return [ShardingType.ROW_WISE.value, ShardingType.TABLE_WISE.value]

    def compute_kernels(
        self, sharding_type: str, compute_device_type: str
    ) -> List[str]:
        return [EmbeddingComputeKernel.FUSED.value]


class TWSharder(EmbeddingBagCollectionSharder, ModuleSharder[nn.Module]):
    def sharding_types(self, compute_device_type: str) -> List[str]:
        return [ShardingType.TABLE_WISE.value]

    def compute_kernels(
        self, sharding_type: str, compute_device_type: str
    ) -> List[str]:
        return [EmbeddingComputeKernel.FUSED.value]


class TestEmbeddingShardingPlanner(unittest.TestCase):
    def setUp(self) -> None:
        compute_device = "cuda"
        self.topology = Topology(
            world_size=2, hbm_cap=1024 * 1024 * 2, compute_device=compute_device
        )
        self.planner = EmbeddingShardingPlanner(topology=self.topology)

    def test_tw_solution(self) -> None:
        tables = [
            EmbeddingBagConfig(
                num_embeddings=100,
                embedding_dim=64,
                name="table_" + str(i),
                feature_names=["feature_" + str(i)],
            )
            for i in range(4)
        ]
        model = TestSparseNN(tables=tables, sparse_device=torch.device("meta"))
        sharding_plan = self.planner.plan(module=model, sharders=[TWvsRWSharder()])
        expected_ranks = [[0], [0], [1], [1]]
        ranks = [
            cast(List[int], param_shard.ranks)
            for param_shard in cast(
                EmbeddingModuleShardingPlan, sharding_plan.plan["sparse.ebc"]
            ).values()
        ]

        self.assertEqual(sorted(expected_ranks), sorted(ranks))

    def test_hidden_rw_solution(self) -> None:
        tables = [
            EmbeddingBagConfig(
                num_embeddings=100,
                embedding_dim=64,
                name="table_" + str(i),
                feature_names=["feature_" + str(i)],
            )
            for i in range(3)
        ]
        model = TestSparseNN(tables=tables, sparse_device=torch.device("meta"))
        sharding_plan = self.planner.plan(module=model, sharders=[TWvsRWSharder()])
        expected_ranks = [[0], [0, 1], [1]]
        ranks = [
            cast(List[int], param_shard.ranks)
            for param_shard in cast(
                EmbeddingModuleShardingPlan, sharding_plan.plan["sparse.ebc"]
            ).values()
        ]

        self.assertEqual(sorted(expected_ranks), sorted(ranks))

    def test_never_fit(self) -> None:
        tables = [
            EmbeddingBagConfig(
                num_embeddings=10000000,
                embedding_dim=10000000,
                name="table_" + str(i),
                feature_names=["feature_" + str(i)],
            )
            for i in range(2)
        ]
        model = TestSparseNN(tables=tables, sparse_device=torch.device("meta"))

        with self.assertRaises(PlannerError) as context:
            self.planner.plan(module=model, sharders=[TWvsRWSharder()])
        self.assertEqual(
            context.exception.error_type, PlannerErrorType.INSUFFICIENT_STORAGE
        )

        # since it has negative storage_constraint
        self.assertEqual(self.planner._num_proposals, 0)

    def test_fail_then_rerun(self) -> None:
        tables = [
            EmbeddingBagConfig(
                num_embeddings=4096,
                embedding_dim=128,
                name="table_" + str(i),
                feature_names=["feature_" + str(i)],
            )
            for i in range(1)
        ]
        model = TestSparseNN(tables=tables, sparse_device=torch.device("meta"))

        with self.assertRaises(PlannerError) as context:
            self.planner.plan(module=model, sharders=[TWSharder()])
        self.assertEqual(
            context.exception.error_type, PlannerErrorType.STRICT_CONSTRAINTS
        )

        sharding_plan = self.planner.plan(module=model, sharders=[TWvsRWSharder()])
        expected_ranks = [[0, 1]]
        ranks = [
            cast(List[int], param_shard.ranks)
            for param_shard in cast(
                EmbeddingModuleShardingPlan, sharding_plan.plan["sparse.ebc"]
            ).values()
        ]

        self.assertEqual(sorted(expected_ranks), sorted(ranks))

    def test_no_sharders(self) -> None:
        tables = [
            EmbeddingBagConfig(
                num_embeddings=100,
                embedding_dim=64,
                name="table_" + str(i),
                feature_names=["feature_" + str(i)],
            )
            for i in range(4)
        ]
        model = TestSparseNN(tables=tables, sparse_device=torch.device("meta"))
        sharding_plan = self.planner.plan(module=model, sharders=[])

        self.assertEqual(sharding_plan, ShardingPlan({}))


class TestEmbeddingShardingPlannerWithConstraints(unittest.TestCase):
    def setUp(self) -> None:
        compute_device = "cuda"
        self.topology = Topology(
            world_size=2, hbm_cap=1024 * 1024 * 2, compute_device=compute_device
        )
        self.tables = [
            EmbeddingBagConfig(
                num_embeddings=100,
                embedding_dim=64,
                name="table_" + str(i),
                feature_names=["feature_" + str(i)],
            )
            for i in range(4)
        ]
        self.constraints = {
            "table_0": ParameterConstraints(
                enforce_hbm=True,
                cache_params=CacheParams(
                    algorithm=CacheAlgorithm.LFU,
                ),
                feature_names=self.tables[0].feature_names,
            ),
            "table_1": ParameterConstraints(
                enforce_hbm=False,
                stochastic_rounding=True,
                feature_names=self.tables[1].feature_names,
            ),
            "table_2": ParameterConstraints(
                bounds_check_mode=BoundsCheckMode.FATAL,
                feature_names=self.tables[2].feature_names,
            ),
            "table_3": ParameterConstraints(
                cache_params=CacheParams(
                    algorithm=CacheAlgorithm.LFU,
                    load_factor=0.1,
                    reserved_memory=1.0,
                    precision=DataType.FP16,
                ),
                feature_names=self.tables[3].feature_names,
            ),
        }
        self.planner = EmbeddingShardingPlanner(
            topology=self.topology, constraints=self.constraints
        )

    def test_fused_paramters_from_constraints(self) -> None:
        model = TestSparseNN(tables=self.tables, sparse_device=torch.device("meta"))
        sharding_plan = self.planner.plan(module=model, sharders=get_default_sharders())

        expected_fused_params = {
            "table_0": (
                CacheParams(
                    algorithm=CacheAlgorithm.LFU,
                    load_factor=None,
                    reserved_memory=None,
                    precision=None,
                ),
                True,
                None,
                None,
            ),
            "table_1": (None, False, True, None),
            "table_2": (None, None, None, BoundsCheckMode.FATAL),
            "table_3": (
                CacheParams(
                    algorithm=CacheAlgorithm.LFU,
                    load_factor=0.1,
                    reserved_memory=1.0,
                    precision=DataType.FP16,
                ),
                None,
                None,
                None,
            ),
        }

        table_names = ["table_" + str(i) for i in range(4)]
        for table in table_names:
            parameter_sharding = cast(
                EmbeddingModuleShardingPlan, sharding_plan.plan["sparse.ebc"]
            )[table]
            self.assertEqual(
                (
                    parameter_sharding.cache_params,
                    parameter_sharding.enforce_hbm,
                    parameter_sharding.stochastic_rounding,
                    parameter_sharding.bounds_check_mode,
                ),
                expected_fused_params[table],
            )

    def test_passing_info_through_constraints(self) -> None:
        model = TestSparseNN(tables=self.tables, sparse_device=torch.device("meta"))
        _ = self.planner.plan(module=model, sharders=get_default_sharders())

        best_plan: Optional[List[ShardingOption]] = self.planner._best_plan
        self.assertIsNotNone(best_plan)

        for table, constraint, sharding_option in zip(
            self.tables, self.constraints.values(), best_plan
        ):
            self.assertEqual(table.name, sharding_option.name)

            self.assertEqual(table.feature_names, sharding_option.feature_names)
            self.assertEqual(table.feature_names, constraint.feature_names)

            self.assertEqual(constraint.cache_params, sharding_option.cache_params)
            self.assertEqual(constraint.enforce_hbm, sharding_option.enforce_hbm)
            self.assertEqual(
                constraint.stochastic_rounding, sharding_option.stochastic_rounding
            )
            self.assertEqual(
                constraint.bounds_check_mode, sharding_option.bounds_check_mode
            )
            self.assertEqual(constraint.is_weighted, sharding_option.is_weighted)


class TestEmbeddingShardingHashPlannerContextInputs(unittest.TestCase):

    def setUp(self) -> None:
        eb_config = EmbeddingBagConfig(
            name="table_0",
            embedding_dim=160,
            num_embeddings=10000,
            feature_names=["f1"],
            data_type=DataType.FP16,
        )
        module = EmbeddingBagCollection(
            tables=[eb_config],
            is_weighted=False,
            device=torch.device(
                "meta"
            ),  # Using meta device for now since only getting search space
        )
        sharders = [EmbeddingBagCollectionSharder()]

        self.topology = Topology(
            local_world_size=8,
            world_size=1,
            compute_device="cuda",
        )
        self.batch_size = 128
        self.enumerator = EmbeddingEnumerator(
            topology=self.topology, batch_size=self.batch_size
        )
        self.enumerator.enumerate(module, sharders)  # pyre-ignore

        self.storage_reservation = HeuristicalStorageReservation(percentage=0.15)
        self.perf_model = NoopPerfModel(topology=self.topology)
        self.constraints = {"table1": ParameterConstraints()}

        self.storage_reservation.reserve(
            topology=self.topology,
            batch_size=self.batch_size,
            module=module,
            sharders=sharders,  # pyre-ignore
            constraints=self.constraints,
        )

    def test_hash_equality(self) -> None:
        planner1 = EmbeddingShardingPlanner(
            topology=self.topology,
            batch_size=self.batch_size,
            enumerator=self.enumerator,
            storage_reservation=self.storage_reservation,
            performance_model=self.perf_model,
            constraints=self.constraints,
        )

        planner2 = EmbeddingShardingPlanner(
            topology=self.topology,
            batch_size=self.batch_size,
            enumerator=self.enumerator,
            storage_reservation=self.storage_reservation,
            performance_model=self.perf_model,
            constraints=self.constraints,
        )

        self.assertEqual(
            planner1.hash_planner_context_inputs(),
            planner2.hash_planner_context_inputs(),
            "Hashes should be equal for identical planners",
        )

    def test_hash_inequality(self) -> None:
        planner1 = EmbeddingShardingPlanner(
            topology=self.topology,
            batch_size=self.batch_size,
            enumerator=self.enumerator,
            storage_reservation=self.storage_reservation,
            performance_model=self.perf_model,
            constraints=self.constraints,
        )

        different_topology = Topology(
            local_world_size=8,
            world_size=2,  # Different world size
            compute_device="cuda",
        )

        planner2 = EmbeddingShardingPlanner(
            topology=different_topology,  # Different topology
            batch_size=self.batch_size * 2,  # Different batch size
            enumerator=self.enumerator,
            storage_reservation=self.storage_reservation,
            performance_model=self.perf_model,
            constraints=self.constraints,
        )

        self.assertNotEqual(
            planner1.hash_planner_context_inputs(),
            planner2.hash_planner_context_inputs(),
            "Hashes should be different for different planners",
        )


class AutoSharder(EmbeddingBagCollectionSharder, ModuleSharder[nn.Module]):
    def sharding_types(self, compute_device_type: str) -> List[str]:
        return [ShardingType.ROW_WISE.value, ShardingType.TABLE_WISE.value]

    def compute_kernels(
        self, sharding_type: str, compute_device_type: str
    ) -> List[str]:
        return [
            k.value
            for k in EmbeddingComputeKernel
            if k is not EmbeddingComputeKernel.CUSTOMIZED_KERNEL
        ]


class TestAutoPlannerWithScaleupProposer(unittest.TestCase):
    def setUp(self) -> None:
        compute_device = "cuda"
        self.topology = Topology(
            world_size=2,
            hbm_cap=1024 * 1024 * 2,
            compute_device=compute_device,
        )
        self.tables = [
            EmbeddingBagConfig(
                num_embeddings=100,
                embedding_dim=64,
                name="table_" + str(i),
                feature_names=["feature_" + str(i)],
            )
            for i in range(4)
        ]
        self.constraints = {
            f"table_{i}": ParameterConstraints(
                # Just needs to be non-None for ScaleupProposer to work.
                cache_params=CacheParams(algorithm=CacheAlgorithm.LRU),
            )
            for i in range(4)
        }
        self.planner = EmbeddingShardingPlanner(
            topology=self.topology,
            proposer=EmbeddingOffloadScaleupProposer(),
            constraints=self.constraints,
        )

    def test_auto_sharder_solution(self) -> None:
        model = TestSparseNN(tables=self.tables, sparse_device=torch.device("meta"))
        sharding_plan = self.planner.plan(module=model, sharders=[AutoSharder()])
        expected_ranks = [[0, 1], [0, 1], [0, 1], [0, 1]]
        ranks = [
            cast(List[int], param_shard.ranks)
            for param_shard in cast(
                EmbeddingModuleShardingPlan, sharding_plan.plan["sparse.ebc"]
            ).values()
        ]
        compute_kernels = {
            param_shard.compute_kernel
            for param_shard in cast(
                EmbeddingModuleShardingPlan, sharding_plan.plan["sparse.ebc"]
            ).values()
        }

        self.assertEqual(sorted(expected_ranks), sorted(ranks))
        self.assertSetEqual(
            {EmbeddingComputeKernel.FUSED_UVM_CACHING.value}, compute_kernels
        )

    def test_planner_with_virtual_table(self) -> None:
        table_count = 4
        tables = [
            EmbeddingConfig(
                num_embeddings=1_125_899_902_955_520,
                embedding_dim=64,
                name="table_" + str(i),
                feature_names=["feature_" + str(i)],
                use_virtual_table=True,
                total_num_buckets=3_991_680,
            )
            for i in range(table_count // 2)
        ] + [
            EmbeddingConfig(
                num_embeddings=100_000,
                embedding_dim=64,
                name="table_" + str(i),
                feature_names=["feature_" + str(i)],
            )
            for i in range(table_count // 2, table_count)
        ]
        model = TestSparseNN(tables=tables, sparse_device=torch.device("meta"))

        constraints = {
            **{
                f"table_{i}": ParameterConstraints(
                    sharding_types=["row_wise"],
                    compute_kernels=["dram_virtual_table"],
                )
                for i in range(table_count // 2)
            },
            **{
                f"table_{i}": ParameterConstraints(
                    cache_params=CacheParams(algorithm=CacheAlgorithm.LRU)
                )
                for i in range(table_count // 2, table_count)
            },
        }

        topology = Topology(
            world_size=2,
            hbm_cap=1024 * 1024 * 1024 * 2,
            ddr_cap=1024 * 1024 * 1024 * 256,
            compute_device="cuda",
        )

        planner = EmbeddingShardingPlanner(
            topology=topology,
            proposer=EmbeddingOffloadScaleupProposer(),
            constraints=constraints,
        )

        sharding_plan = planner.plan(
            module=model,
            sharders=[EmbeddingCollectionSharder()],  # pyre-ignore
        )

        for table_index in range(4):
            # pyre-ignore
            shards = sharding_plan.plan["sparse.ec"][
                f"table_{table_index}"
            ].sharding_spec.shards
            self.assertEqual(len(shards), 2)
            self.assertEqual(shards[0].shard_offsets, [0, 0])
            self.assertEqual(
                shards[0].shard_sizes,
                [562949951477760 if table_index < 2 else 50_000, 64],
            )
            self.assertEqual(
                shards[1].shard_offsets,
                [562949951477760 if table_index < 2 else 50_000, 0],
            )
            self.assertEqual(
                shards[1].shard_sizes,
                [562949951477760 if table_index < 2 else 50_000, 64],
            )
        stats: List[str] = cast(EmbeddingStats, planner._stats[0])._stats_table
        # L1 cache size is 64GB per shard and L2 cache size is 128MB per shard per table
        self.assertTrue(
            any(
                "dram_virtual_table: HBM: 0.001 GB, DDR: 0.0 GB" in line
                for line in stats
            )
        )
        self.assertTrue(
            any(
                "fused_uvm_caching: HBM: 0.011 GB, DDR: 0.048 GB" in line
                for line in stats
            )
        )
        self.assertTrue(
            any("Max HBM: 0.006 GB on ranks [0, 1]" in line for line in stats)
        )
        self.assertTrue(
            any("Max HBM: 0.006 GB on ranks [0, 1]" in line for line in stats)
        )

        constraints = {
            **{
                f"table_{i}": ParameterConstraints(
                    sharding_types=["row_wise"],
                    compute_kernels=["dram_virtual_table"],
                    key_value_params=KeyValueParams(
                        l2_cache_size=64, max_l1_cache_size=128
                    ),
                )
                for i in range(table_count // 2)
            },
            **{
                f"table_{i}": ParameterConstraints(
                    cache_params=CacheParams(algorithm=CacheAlgorithm.LRU),
                )
                for i in range(table_count // 2, table_count)
            },
        }

        topology = Topology(
            world_size=2,
            hbm_cap=1024 * 1024 * 1024 * 2,
            ddr_cap=1024 * 1024 * 1024 * 256,
            compute_device="cuda",
        )

        planner = EmbeddingShardingPlanner(
            topology=topology,
            proposer=EmbeddingOffloadScaleupProposer(),
            constraints=constraints,
        )
        sharding_plan = planner.plan(
            module=model, sharders=[EmbeddingCollectionSharder()]  # pyre-ignore
        )

        expected_ranks = [[0, 1], [0, 1], [0, 1], [0, 1]]
        ranks = [
            cast(List[int], param_shard.ranks)
            for param_shard in cast(
                EmbeddingModuleShardingPlan, sharding_plan.plan["sparse.ec"]
            ).values()
        ]
        compute_kernels = {
            param_shard.compute_kernel
            for param_shard in cast(
                EmbeddingModuleShardingPlan, sharding_plan.plan["sparse.ec"]
            ).values()
        }
        self.assertEqual(sorted(expected_ranks), sorted(ranks))
        self.assertSetEqual(
            {
                EmbeddingComputeKernel.DRAM_VIRTUAL_TABLE.value,
                EmbeddingComputeKernel.FUSED_UVM_CACHING.value,
            },
            compute_kernels,
        )

        for table_index in range(4):
            shards = sharding_plan.plan["sparse.ec"][
                f"table_{table_index}"
            ].sharding_spec.shards
            self.assertEqual(len(shards), 2)
            self.assertEqual(shards[0].shard_offsets, [0, 0])
            self.assertEqual(
                shards[0].shard_sizes,
                [562949951477760 if table_index < 2 else 50_000, 64],
            )
            self.assertEqual(
                shards[1].shard_offsets,
                [562949951477760 if table_index < 2 else 50_000, 0],
            )
            self.assertEqual(
                shards[1].shard_sizes,
                [562949951477760 if table_index < 2 else 50_000, 64],
            )
        stats: List[str] = cast(EmbeddingStats, planner._stats[0])._stats_table
        # L1 cache size is 64GB per shard and L2 cache size is 128MB per shard per table
        self.assertTrue(
            any(
                "dram_virtual_table: HBM: 0.501 GB, DDR: 0.0 GB" in line
                for line in stats
            )
        )
        self.assertTrue(
            any(
                "fused_uvm_caching: HBM: 0.011 GB, DDR: 0.048 GB" in line
                for line in stats
            )
        )
        self.assertTrue(
            any("Max HBM: 0.256 GB on ranks [0, 1]" in line for line in stats)
        )
        self.assertTrue(
            any("Min HBM: 0.256 GB on ranks [0, 1]" in line for line in stats)
        )

        constraints = {
            **{
                f"table_{i}": ParameterConstraints(
                    sharding_types=["row_wise"],
                    compute_kernels=["dram_virtual_table"],
                    key_value_params=KeyValueParams(
                        l2_cache_size=64, max_l1_cache_size=128
                    ),
                )
                for i in range(table_count // 2)
            },
            **{
                f"table_{i}": ParameterConstraints(
                    cache_params=CacheParams(algorithm=CacheAlgorithm.LRU),
                )
                for i in range(table_count // 2, table_count)
            },
        }

        topology = Topology(
            world_size=2,
            hbm_cap=1024 * 1024 * 1024 * 2,
            ddr_cap=1024 * 1024 * 1024 * 256,
            compute_device="cuda",
        )

        planner = EmbeddingShardingPlanner(
            topology=topology,
            proposer=EmbeddingOffloadScaleupProposer(),
            constraints=constraints,
        )
        sharding_plan = planner.plan(
            module=model, sharders=[EmbeddingCollectionSharder()]  # pyre-ignore
        )

        expected_ranks = [[0, 1], [0, 1], [0, 1], [0, 1]]
        ranks = [
            cast(List[int], param_shard.ranks)
            for param_shard in cast(
                EmbeddingModuleShardingPlan, sharding_plan.plan["sparse.ec"]
            ).values()
        ]
        compute_kernels = {
            param_shard.compute_kernel
            for param_shard in cast(
                EmbeddingModuleShardingPlan, sharding_plan.plan["sparse.ec"]
            ).values()
        }
        self.assertEqual(sorted(expected_ranks), sorted(ranks))
        self.assertSetEqual(
            {
                EmbeddingComputeKernel.DRAM_VIRTUAL_TABLE.value,
                EmbeddingComputeKernel.FUSED_UVM_CACHING.value,
            },
            compute_kernels,
        )

        tables = [
            EmbeddingConfig(
                num_embeddings=10000,
                embedding_dim=64,
                name="table_" + str(i),
                feature_names=["feature_" + str(i)],
                use_virtual_table=True,
                total_num_buckets=10,
            )
            for i in range(table_count // 2)
        ] + [
            EmbeddingConfig(
                num_embeddings=100_000,
                embedding_dim=64,
                name="table_" + str(i),
                feature_names=["feature_" + str(i)],
            )
            for i in range(table_count // 2, table_count)
        ]

        model = TestSparseNN(tables=tables, sparse_device=torch.device("meta"))

        planner = EmbeddingShardingPlanner(
            topology=topology,
            proposer=EmbeddingOffloadScaleupProposer(),
            constraints=constraints,
        )

        #  L1 cache size > size of embedding table * default cache load factor

        sharding_plan = planner.plan(
            module=model, sharders=[EmbeddingCollectionSharder()]  # pyre-ignore
        )
        for table_index in range(4):
            shards = sharding_plan.plan["sparse.ec"][
                f"table_{table_index}"
            ].sharding_spec.shards
            self.assertEqual(len(shards), 2)
            self.assertEqual(shards[0].shard_offsets, [0, 0])
            self.assertEqual(
                shards[0].shard_sizes,
                [5000 if table_index < 2 else 50_000, 64],
            )
            self.assertEqual(
                shards[1].shard_offsets,
                [5000 if table_index < 2 else 50_000, 0],
            )
            self.assertEqual(
                shards[1].shard_sizes,
                [5000 if table_index < 2 else 50_000, 64],
            )
        stats: List[str] = cast(EmbeddingStats, planner._stats[0])._stats_table
        # L1 cache size of 64GB > size of embedding table * cache load factor. We use the smaller value.
        # L2 cache size is 128MB per shard per table
        self.assertTrue(
            any(
                "dram_virtual_table: HBM: 0.002 GB, DDR: 0.0 GB" in line
                for line in stats
            )
        )
        self.assertTrue(
            any(
                "fused_uvm_caching: HBM: 0.011 GB, DDR: 0.048 GB" in line
                for line in stats
            )
        )
        self.assertTrue(
            any("Max HBM: 0.007 GB on ranks [0, 1]" in line for line in stats)
        )
        self.assertTrue(
            any("Min HBM: 0.007 GB on ranks [0, 1]" in line for line in stats)
        )

        # Override cache load factor
        planner = EmbeddingShardingPlanner(
            topology=topology,
            proposer=EmbeddingOffloadScaleupProposer(),
            constraints=constraints,
        )
        sharding_plan = planner.plan(
            module=model,
            sharders=[  # pyre-ignore
                EmbeddingCollectionSharder(fused_params={"cache_load_factor": 0.5})
            ],
        )
        for table_index in range(4):
            shards = sharding_plan.plan["sparse.ec"][
                f"table_{table_index}"
            ].sharding_spec.shards
            self.assertEqual(len(shards), 2)
            self.assertEqual(shards[0].shard_offsets, [0, 0])
            self.assertEqual(
                shards[0].shard_sizes,
                [5000 if table_index < 2 else 50_000, 64],
            )
            self.assertEqual(
                shards[1].shard_offsets,
                [5000 if table_index < 2 else 50_000, 0],
            )
            self.assertEqual(
                shards[1].shard_sizes,
                [5000 if table_index < 2 else 50_000, 64],
            )
        stats: List[str] = cast(EmbeddingStats, planner._stats[0])._stats_table
        # L1 cache size of 64GB > size of embedding table * cache load factor. We use the smaller value.
        # L2 cache size is 128MB per shard per table
        self.assertTrue(
            any(
                "dram_virtual_table: HBM: 0.005 GB, DDR: 0.0 GB" in line
                for line in stats
            )
        )
        self.assertTrue(
            any(
                "fused_uvm_caching: HBM: 0.027 GB, DDR: 0.048 GB" in line
                for line in stats
            )
        )
        self.assertTrue(
            any("Max HBM: 0.016 GB on ranks [0, 1]" in line for line in stats)
        )
        self.assertTrue(
            any("Min HBM: 0.016 GB on ranks [0, 1]" in line for line in stats)
        )
