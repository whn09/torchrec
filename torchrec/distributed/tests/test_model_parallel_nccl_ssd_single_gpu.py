#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

import copy
import unittest
from typing import cast, List, OrderedDict, Union

import torch
import torch.distributed as dist
import torch.nn as nn
from fbgemm_gpu.split_embedding_configs import EmbOptimType
from hypothesis import given, settings, strategies as st, Verbosity
from torchrec.distributed.batched_embedding_kernel import (
    KeyValueEmbedding,
    KeyValueEmbeddingBag,
    ZeroCollisionKeyValueEmbedding,
    ZeroCollisionKeyValueEmbeddingBag,
)
from torchrec.distributed.embedding_types import (
    EmbeddingComputeKernel,
    ShardedEmbeddingTable,
)
from torchrec.distributed.embeddingbag import EmbeddingBagCollectionSharder
from torchrec.distributed.model_parallel import DistributedModelParallel
from torchrec.distributed.planner import ParameterConstraints
from torchrec.distributed.test_utils.test_model import TestSparseNN
from torchrec.distributed.test_utils.test_model_parallel_base import (
    ModelParallelSingleRankBase,
)
from torchrec.distributed.test_utils.test_sharding import (
    copy_state_dict,
    create_test_sharder,
    SharderType,
    sharding_single_rank_test_single_process,
)
from torchrec.distributed.tests.test_sequence_model import (
    TestEmbeddingCollectionSharder,
    TestSequenceSparseNN,
)
from torchrec.distributed.types import KeyValueParams, ModuleSharder, ShardingType
from torchrec.modules.embedding_configs import (
    DataType,
    EmbeddingBagConfig,
    EmbeddingConfig,
)
from torchrec.optim import RowWiseAdagrad


def _load_split_embedding_weights(
    emb_module: Union[KeyValueEmbedding, KeyValueEmbeddingBag],
    weights: List[torch.Tensor],
) -> None:
    """
    Util function to set the weights of SSD TBE.
    """
    embedding_tables: List[ShardedEmbeddingTable] = emb_module.config.embedding_tables

    assert len(weights) == len(
        embedding_tables
    ), "Expect length of weights to be equal to number of embedding tables. "

    cum_sum = 0
    for table_id, (table, weight) in enumerate(zip(embedding_tables, weights)):
        # load weights for SSD TBE
        height = weight.shape[0]
        shard_shape = table.local_rows, table.local_cols
        assert shard_shape == weight.shape, "Expect shard shape to match tensor shape."
        assert weight.device == torch.device("cpu"), "Weight has to be on CPU."
        emb_module.emb_module.ssd_db.set_cuda(
            torch.arange(cum_sum, cum_sum + height, dtype=torch.int64),
            weight,
            torch.as_tensor([height]),
            table_id,
        )
        cum_sum += height


class KeyValueModelParallelTest(ModelParallelSingleRankBase):
    def _create_tables(self) -> None:
        num_features = 4
        self.tables += [
            EmbeddingBagConfig(
                num_embeddings=(i + 1) * 1000,
                embedding_dim=256,
                name="table_" + str(i),
                feature_names=["feature_" + str(i)],
            )
            for i in range(num_features)
        ]

    @staticmethod
    def _copy_ssd_emb_modules(
        m1: DistributedModelParallel, m2: DistributedModelParallel
    ) -> None:
        """
        Util function to copy and set the SSD TBE modules of two models. It
        requires both DMP modules to have the same sharding plan.
        """
        for lookup1, lookup2 in zip(
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ebc`.
            m1.module.sparse.ebc._lookups,
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ebc`.
            m2.module.sparse.ebc._lookups,
        ):
            for emb_module1, emb_module2 in zip(
                lookup1._emb_modules, lookup2._emb_modules
            ):
                ssd_emb_modules = {KeyValueEmbeddingBag, KeyValueEmbedding}
                if type(emb_module1) in ssd_emb_modules:
                    assert type(emb_module1) is type(emb_module2), (
                        "Expect two emb_modules to be of the same type, either both "
                        "SSDEmbeddingBag or SSDEmbeddingBag."
                    )

                    emb1_kv = {
                        t: pmt
                        for t, pmt, _, _ in emb_module1.get_named_split_embedding_weights_snapshot()
                    }
                    for (
                        t,
                        pmt,
                        _,
                        _,
                    ) in emb_module2.get_named_split_embedding_weights_snapshot():
                        pmt1 = emb1_kv[t]
                        w1 = pmt1.full_tensor()

                        # write value into ssd for both emb module for later comparison
                        pmt.wrapped.set_range(0, 0, w1.size(0), w1)

                    # purge after loading. This is needed, since we pass a batch
                    # through dmp when instantiating them.
                    emb_module1.purge()
                    emb_module2.purge()

    @unittest.skipIf(
        not torch.cuda.is_available(),
        "Not enough GPUs, this test requires at least one GPU",
    )
    # pyre-ignore[56]
    @given(
        sharder_type=st.sampled_from(
            [
                SharderType.EMBEDDING_BAG_COLLECTION.value,
            ]
        ),
        kernel_type=st.sampled_from(
            [
                EmbeddingComputeKernel.KEY_VALUE.value,
            ]
        ),
        sharding_type=st.sampled_from(
            [
                ShardingType.TABLE_WISE.value,
                # TODO: uncomment when ssd ckpt support cw sharding
                # ShardingType.COLUMN_WISE.value,
                ShardingType.ROW_WISE.value,
                ShardingType.TABLE_ROW_WISE.value,
                # TODO: uncomment when ssd ckpt support cw sharding
                # ShardingType.TABLE_COLUMN_WISE.value,
            ]
        ),
        is_training=st.booleans(),
        stochastic_rounding=st.booleans(),
        dtype=st.sampled_from([DataType.FP32, DataType.FP16]),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=4, deadline=None)
    def test_ssd_load_state_dict(
        self,
        sharder_type: str,
        kernel_type: str,
        sharding_type: str,
        is_training: bool,
        stochastic_rounding: bool,
        dtype: DataType,
    ) -> None:
        """
        This test checks that if SSD TBE is deterministic. That is, if two SSD
        TBEs start with the same state, they would produce the same output.
        """
        self._set_table_weights_precision(dtype)

        fused_params = {
            "learning_rate": 0.1,
            "stochastic_rounding": stochastic_rounding,
        }
        is_deterministic = dtype == DataType.FP32 or not stochastic_rounding
        constraints = {
            table.name: ParameterConstraints(
                sharding_types=[sharding_type],
                compute_kernels=[kernel_type],
                key_value_params=KeyValueParams(bulk_init_chunk_size=1024),
            )
            for i, table in enumerate(self.tables)
        }
        sharders = [
            create_test_sharder(
                sharder_type,
                sharding_type,
                kernel_type,
                fused_params=fused_params,
            ),
        ]

        # pyre-ignore
        models, batch = self._generate_dmps_and_batch(sharders, constraints=constraints)
        m1, m2 = models

        # load state dict for dense modules
        m2.load_state_dict(cast("OrderedDict[str, torch.Tensor]", m1.state_dict()))
        self._copy_ssd_emb_modules(m1, m2)

        if is_training:
            self._train_models(m1, m2, batch)
        self._eval_models(m1, m2, batch, is_deterministic=is_deterministic)
        self._compare_models(m1, m2, is_deterministic=is_deterministic)

    @unittest.skipIf(
        not torch.cuda.is_available(),
        "Not enough GPUs, this test requires at least one GPU",
    )
    # pyre-ignore[56]
    @given(
        sharder_type=st.sampled_from(
            [
                SharderType.EMBEDDING_BAG_COLLECTION.value,
            ]
        ),
        kernel_type=st.sampled_from(
            [
                EmbeddingComputeKernel.KEY_VALUE.value,
            ]
        ),
        sharding_type=st.sampled_from(
            [
                ShardingType.TABLE_WISE.value,
                # TODO: uncomment when ssd ckpt support cw sharding
                # ShardingType.COLUMN_WISE.value,
                ShardingType.ROW_WISE.value,
                ShardingType.TABLE_ROW_WISE.value,
                # TODO: uncomment when ssd ckpt support cw sharding
                # ShardingType.TABLE_COLUMN_WISE.value,
            ]
        ),
        is_training=st.booleans(),
        stochastic_rounding=st.booleans(),
        dtype=st.sampled_from([DataType.FP32, DataType.FP16]),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=4, deadline=None)
    def test_ssd_tbe_numerical_accuracy(
        self,
        sharder_type: str,
        kernel_type: str,
        sharding_type: str,
        is_training: bool,
        stochastic_rounding: bool,
        dtype: DataType,
    ) -> None:
        """
        Make sure it produces same numbers as normal TBE.
        """
        self._set_table_weights_precision(dtype)

        base_kernel_type = EmbeddingComputeKernel.FUSED.value
        learning_rate = 0.1
        fused_params = {
            "optimizer": EmbOptimType.EXACT_ROWWISE_ADAGRAD,
            "learning_rate": learning_rate,
            "stochastic_rounding": stochastic_rounding,
        }
        is_deterministic = dtype == DataType.FP32 or not stochastic_rounding
        fused_sharders = [
            cast(
                ModuleSharder[nn.Module],
                create_test_sharder(
                    sharder_type,
                    sharding_type,
                    base_kernel_type,  # base kernel type
                    fused_params=fused_params,
                ),
            ),
        ]
        ssd_sharders = [
            cast(
                ModuleSharder[nn.Module],
                create_test_sharder(
                    sharder_type,
                    sharding_type,
                    kernel_type,
                    fused_params=fused_params,
                ),
            ),
        ]
        ssd_constraints = {
            table.name: ParameterConstraints(
                sharding_types=[sharding_type],
                compute_kernels=[kernel_type],
            )
            for i, table in enumerate(self.tables)
        }
        (fused_model, _), _ = self._generate_dmps_and_batch(fused_sharders)
        (ssd_model, _), batch = self._generate_dmps_and_batch(
            ssd_sharders, constraints=ssd_constraints
        )

        # load state dict for dense modules
        copy_state_dict(
            ssd_model.state_dict(), fused_model.state_dict(), exclude_predfix="sparse"
        )

        # for this to work, we expect the order of lookups to be the same
        # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ebc`.
        assert len(fused_model.module.sparse.ebc._lookups) == len(
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ebc`.
            ssd_model.module.sparse.ebc._lookups
        ), "Expect same number of lookups"

        for fused_lookup, ssd_lookup in zip(
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ebc`.
            fused_model.module.sparse.ebc._lookups,
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ebc`.
            ssd_model.module.sparse.ebc._lookups,
        ):
            assert len(fused_lookup._emb_modules) == len(
                ssd_lookup._emb_modules
            ), "Expect same number of emb modules"
            for fused_emb_module, ssd_emb_module in zip(
                fused_lookup._emb_modules, ssd_lookup._emb_modules
            ):
                weights = fused_emb_module.split_embedding_weights()
                weights = [weight.to("cpu") for weight in weights]
                _load_split_embedding_weights(ssd_emb_module, weights)

                # purge after loading. This is needed, since we pass a batch
                # through dmp when instantiating them.
                ssd_emb_module.purge()

        if is_training:
            self._train_models(fused_model, ssd_model, batch)
        self._eval_models(
            fused_model, ssd_model, batch, is_deterministic=is_deterministic
        )

    @unittest.skipIf(
        not torch.cuda.is_available(),
        "Not enough GPUs, this test requires at least one GPU",
    )
    # pyre-ignore[56]
    @given(
        sharder_type=st.sampled_from(
            [
                SharderType.EMBEDDING_BAG_COLLECTION.value,
            ]
        ),
        kernel_type=st.sampled_from(
            [
                EmbeddingComputeKernel.KEY_VALUE.value,
            ]
        ),
        sharding_type=st.sampled_from(
            [
                ShardingType.TABLE_WISE.value,
                # TODO: uncomment when ssd ckpt support cw sharding
                # ShardingType.COLUMN_WISE.value,
                ShardingType.ROW_WISE.value,
                ShardingType.TABLE_ROW_WISE.value,
                # TODO: uncomment when ssd ckpt support cw sharding
                # ShardingType.TABLE_COLUMN_WISE.value,
            ]
        ),
        is_training=st.booleans(),
        stochastic_rounding=st.booleans(),
        dtype=st.sampled_from([DataType.FP32, DataType.FP16]),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=4, deadline=None)
    def test_ssd_fused_optimizer(
        self,
        sharder_type: str,
        kernel_type: str,
        sharding_type: str,
        is_training: bool,
        stochastic_rounding: bool,
        dtype: DataType,
    ) -> None:
        """
        Purpose of this test is to make sure it works with warm up policy.
        """
        self._set_table_weights_precision(dtype)

        is_deterministic = dtype == DataType.FP32 or not stochastic_rounding

        constraints = {
            table.name: ParameterConstraints(
                sharding_types=[sharding_type],
                compute_kernels=[kernel_type],
                key_value_params=KeyValueParams(bulk_init_chunk_size=1024),
            )
            for i, table in enumerate(self.tables)
        }

        base_sharders = [
            create_test_sharder(
                sharder_type,
                sharding_type,
                kernel_type,
                fused_params={
                    "learning_rate": 0.2,
                    "stochastic_rounding": stochastic_rounding,
                },
            ),
        ]
        models, batch = self._generate_dmps_and_batch(
            base_sharders,  # pyre-ignore
            constraints=constraints,
        )
        base_model, _ = models

        test_sharders = [
            create_test_sharder(
                sharder_type,
                sharding_type,
                kernel_type,
                fused_params={
                    "learning_rate": 0.1,
                    "stochastic_rounding": stochastic_rounding,
                },
            ),
        ]
        models, _ = self._generate_dmps_and_batch(
            test_sharders,  # pyre-ignore
            constraints=constraints,
        )
        test_model, _ = models

        # load state dict for dense modules
        test_model.load_state_dict(
            cast("OrderedDict[str, torch.Tensor]", base_model.state_dict())
        )
        self._copy_ssd_emb_modules(base_model, test_model)

        self._eval_models(
            base_model, test_model, batch, is_deterministic=is_deterministic
        )

        # change learning rate for test_model
        fused_opt = test_model.fused_optimizer
        # pyre-ignore
        fused_opt.param_groups[0]["lr"] = 0.2
        fused_opt.zero_grad()

        if is_training:
            self._train_models(base_model, test_model, batch)
        self._eval_models(
            base_model, test_model, batch, is_deterministic=is_deterministic
        )
        self._compare_models(base_model, test_model, is_deterministic=is_deterministic)

    @unittest.skipIf(
        not torch.cuda.is_available(),
        "Not enough GPUs, this test requires at least one GPU",
    )
    # pyre-ignore[56]
    @given(
        sharder_type=st.sampled_from(
            [
                SharderType.EMBEDDING_BAG_COLLECTION.value,
            ]
        ),
        kernel_type=st.sampled_from(
            [
                EmbeddingComputeKernel.KEY_VALUE.value,
            ]
        ),
        sharding_type=st.sampled_from(
            [
                ShardingType.TABLE_WISE.value,
                # TODO: uncomment when ssd ckpt support cw sharding
                # ShardingType.COLUMN_WISE.value,
                ShardingType.ROW_WISE.value,
                ShardingType.TABLE_ROW_WISE.value,
                # TODO: uncomment when ssd ckpt support cw sharding
                # ShardingType.TABLE_COLUMN_WISE.value,
            ]
        ),
        is_training=st.booleans(),
        stochastic_rounding=st.booleans(),
        dtype=st.sampled_from([DataType.FP32, DataType.FP16]),
        fused_first=st.booleans(),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=4, deadline=None)
    def test_ssd_mixed_kernels(
        self,
        sharder_type: str,
        kernel_type: str,
        sharding_type: str,
        is_training: bool,
        stochastic_rounding: bool,
        dtype: DataType,
        fused_first: bool,
    ) -> None:
        """
        Purpose of this test is to make sure it works with warm up policy.
        """
        self._set_table_weights_precision(dtype)

        base_kernel_type = EmbeddingComputeKernel.FUSED.value

        is_deterministic = dtype == DataType.FP32 or not stochastic_rounding

        constraints = {
            table.name: ParameterConstraints(
                sharding_types=[sharding_type],
                compute_kernels=(
                    [base_kernel_type] if i % 2 == fused_first else [kernel_type]
                ),
                key_value_params=KeyValueParams(bulk_init_chunk_size=1024),
            )
            for i, table in enumerate(self.tables)
        }

        fused_params = {
            "learning_rate": 0.1,
            "stochastic_rounding": stochastic_rounding,
        }
        sharders = [
            EmbeddingBagCollectionSharder(fused_params=fused_params),
        ]

        # pyre-ignore
        models, batch = self._generate_dmps_and_batch(sharders, constraints=constraints)
        m1, m2 = models

        # load state dict for dense modules
        m2.load_state_dict(cast("OrderedDict[str, torch.Tensor]", m1.state_dict()))
        self._copy_ssd_emb_modules(m1, m2)

        if is_training:
            self._train_models(m1, m2, batch)
        self._eval_models(m1, m2, batch, is_deterministic=is_deterministic)
        self._compare_models(m1, m2, is_deterministic=is_deterministic)

    @unittest.skipIf(
        not torch.cuda.is_available(),
        "Not enough GPUs, this test requires at least one GPU",
    )
    # pyre-fixme[56]
    @given(
        sharding_type=st.sampled_from(
            [
                ShardingType.TABLE_WISE.value,
                # TODO: uncomment when ssd ckpt support cw sharding
                # ShardingType.COLUMN_WISE.value,
                ShardingType.ROW_WISE.value,
                ShardingType.TABLE_ROW_WISE.value,
                # TODO: uncomment when ssd ckpt support cw sharding
                # ShardingType.TABLE_COLUMN_WISE.value,
            ]
        ),
        dtype=st.sampled_from([DataType.FP32, DataType.FP16]),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=6, deadline=None)
    def test_ssd_mixed_kernels_with_vbe(
        self,
        sharding_type: str,
        dtype: DataType,
    ) -> None:
        self._set_table_weights_precision(dtype)
        fused_params = {
            "prefetch_pipeline": True,
        }
        constraints = {
            table.name: ParameterConstraints(
                min_partition=4,
                compute_kernels=(
                    [EmbeddingComputeKernel.FUSED.value]
                    if i % 2 == 0
                    else [EmbeddingComputeKernel.KEY_VALUE.value]
                ),
                sharding_types=[sharding_type],
            )
            for i, table in enumerate(self.tables)
        }
        optimizer_config = (RowWiseAdagrad, {"lr": 0.001, "eps": 0.001})
        pg = dist.GroupMember.WORLD

        assert pg is not None, "Process group is not initialized"
        sharding_single_rank_test_single_process(
            pg=pg,
            device=self.device,
            rank=0,
            world_size=1,
            # pyre-fixme[6]: The intake type should be `type[TestSparseNNBase]`
            model_class=TestSparseNN,
            embedding_groups={},
            tables=self.tables,
            # pyre-fixme[6]
            sharders=[EmbeddingBagCollectionSharder(fused_params=fused_params)],
            optim=EmbOptimType.EXACT_SGD,
            # The optimizer config here will overwrite the SGD optimizer above
            apply_optimizer_in_backward_config={
                "embedding_bags": optimizer_config,
                "embeddings": optimizer_config,
            },
            constraints=constraints,
            variable_batch_per_feature=True,
            random_seed=100,
        )

    @unittest.skipIf(
        not torch.cuda.is_available(),
        "Not enough GPUs, this test requires at least one GPU",
    )
    # pyre-ignore[56]
    @given(
        sharder_type=st.sampled_from(
            [
                SharderType.EMBEDDING_BAG_COLLECTION.value,
            ]
        ),
        kernel_type=st.sampled_from(
            [
                EmbeddingComputeKernel.KEY_VALUE.value,
            ]
        ),
        is_training=st.booleans(),
        stochastic_rounding=st.booleans(),
        dtype=st.sampled_from([DataType.FP32, DataType.FP16]),
        table_wise_first=st.booleans(),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=4, deadline=None)
    def test_ssd_mixed_sharding_types(
        self,
        sharder_type: str,
        kernel_type: str,
        is_training: bool,
        stochastic_rounding: bool,
        dtype: DataType,
        table_wise_first: bool,
    ) -> None:
        """
        Purpose of this test is to make sure it works with warm up policy.
        """
        self._set_table_weights_precision(dtype)

        is_deterministic = dtype == DataType.FP32 or not stochastic_rounding

        constraints = {
            table.name: ParameterConstraints(
                sharding_types=(
                    [ShardingType.TABLE_WISE.value]
                    if i % 2 == table_wise_first
                    else [ShardingType.ROW_WISE.value]
                ),
                compute_kernels=[kernel_type],
                key_value_params=KeyValueParams(bulk_init_chunk_size=1024),
            )
            for i, table in enumerate(self.tables)
        }

        fused_params = {
            "learning_rate": 0.1,
            "stochastic_rounding": stochastic_rounding,
        }
        sharders = [
            EmbeddingBagCollectionSharder(fused_params=fused_params),
        ]

        # pyre-ignore
        models, batch = self._generate_dmps_and_batch(sharders, constraints=constraints)
        m1, m2 = models

        # load state dict for dense modules
        m2.load_state_dict(cast("OrderedDict[str, torch.Tensor]", m1.state_dict()))
        self._copy_ssd_emb_modules(m1, m2)

        if is_training:
            self._train_models(m1, m2, batch)
        self._eval_models(m1, m2, batch, is_deterministic=is_deterministic)
        self._compare_models(m1, m2, is_deterministic=is_deterministic)


class KeyValueSequenceModelParallelStateDictTest(ModelParallelSingleRankBase):
    def setUp(self, backend: str = "nccl") -> None:
        self.shared_features = []
        self.embedding_groups = {}

        super().setUp(backend=backend)

    def _create_tables(self) -> None:
        num_features = 4
        shared_features = 2

        initial_tables = [
            EmbeddingConfig(
                num_embeddings=(i + 1) * 1000,
                embedding_dim=16,
                name="table_" + str(i),
                feature_names=["feature_" + str(i)],
            )
            for i in range(num_features)
        ]

        shared_features_tables = [
            EmbeddingConfig(
                num_embeddings=(i + 1) * 11,
                embedding_dim=16,
                name="table_" + str(i + num_features),
                feature_names=["feature_" + str(i)],
            )
            for i in range(shared_features)
        ]

        self.tables += initial_tables + shared_features_tables
        self.shared_features += [f"feature_{i}" for i in range(shared_features)]

        self.embedding_groups["group_0"] = [
            (f"{feature}@{table.name}" if feature in self.shared_features else feature)
            for table in self.tables
            for feature in table.feature_names
        ]

    def _create_model(self) -> nn.Module:
        return TestSequenceSparseNN(
            tables=self.tables,
            num_float_features=self.num_float_features,
            embedding_groups=self.embedding_groups,
            dense_device=self.device,
            sparse_device=torch.device("meta"),
        )

    @staticmethod
    def _copy_ssd_emb_modules(
        m1: DistributedModelParallel, m2: DistributedModelParallel
    ) -> None:
        """
        Util function to copy and set the SSD TBE modules of two models. It
        requires both DMP modules to have the same sharding plan.
        """
        for lookup1, lookup2 in zip(
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ec`.
            m1.module.sparse.ec._lookups,
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ec`.
            m2.module.sparse.ec._lookups,
        ):
            for emb_module1, emb_module2 in zip(
                lookup1._emb_modules, lookup2._emb_modules
            ):
                ssd_emb_modules = {KeyValueEmbeddingBag, KeyValueEmbedding}
                if type(emb_module1) in ssd_emb_modules:
                    assert type(emb_module1) is type(emb_module2), (
                        "Expect two emb_modules to be of the same type, either both "
                        "SSDEmbeddingBag or SSDEmbeddingBag."
                    )

                    emb1_kv = {
                        t: pmt
                        for t, pmt, _, _ in emb_module1.get_named_split_embedding_weights_snapshot()
                    }
                    for (
                        t,
                        pmt,
                        _,
                        _,
                    ) in emb_module2.get_named_split_embedding_weights_snapshot():
                        pmt1 = emb1_kv[t]
                        w1 = pmt1.full_tensor()

                        # write value into ssd for both emb module for later comparison
                        pmt.wrapped.set_range(0, 0, w1.size(0), w1)

                    # purge after loading. This is needed, since we pass a batch
                    # through dmp when instantiating them.
                    emb_module1.purge()
                    emb_module2.purge()

    @unittest.skipIf(
        not torch.cuda.is_available(),
        "Not enough GPUs, this test requires at least one GPU",
    )
    # pyre-ignore[56]
    @given(
        sharding_type=st.sampled_from(
            [
                ShardingType.TABLE_WISE.value,
                # TODO: uncomment when ssd ckpt support cw sharding
                # ShardingType.COLUMN_WISE.value,
                ShardingType.ROW_WISE.value,
            ]
        ),
        kernel_type=st.sampled_from(
            [
                EmbeddingComputeKernel.KEY_VALUE.value,
            ]
        ),
        is_training=st.booleans(),
        stochastic_rounding=st.booleans(),
        dtype=st.sampled_from([DataType.FP32, DataType.FP16]),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=4, deadline=None)
    def test_ssd_load_state_dict(
        self,
        sharding_type: str,
        kernel_type: str,
        is_training: bool,
        stochastic_rounding: bool,
        dtype: DataType,
    ) -> None:
        """
        This test checks that if SSD TBE is deterministic. That is, if two SSD
        TBEs start with the same state, they would produce the same output.
        """
        self._set_table_weights_precision(dtype)

        fused_params = {
            "learning_rate": 0.1,
            "stochastic_rounding": stochastic_rounding,
        }
        is_deterministic = dtype == DataType.FP32 or not stochastic_rounding
        sharders = [
            cast(
                ModuleSharder[nn.Module],
                TestEmbeddingCollectionSharder(
                    sharding_type=sharding_type,
                    kernel_type=kernel_type,
                    fused_params=fused_params,
                ),
            ),
        ]

        constraints = {
            table.name: ParameterConstraints(
                sharding_types=[sharding_type],
                compute_kernels=[kernel_type],
                key_value_params=KeyValueParams(bulk_init_chunk_size=1024),
            )
            for i, table in enumerate(self.tables)
        }

        models, batch = self._generate_dmps_and_batch(sharders, constraints=constraints)
        m1, m2 = models

        # load state dict for dense modules
        m2.load_state_dict(cast("OrderedDict[str, torch.Tensor]", m1.state_dict()))
        self._copy_ssd_emb_modules(m1, m2)

        if is_training:
            self._train_models(m1, m2, batch)
        self._eval_models(m1, m2, batch, is_deterministic=is_deterministic)
        self._compare_models(m1, m2, is_deterministic=is_deterministic)


class ZeroCollisionModelParallelTest(ModelParallelSingleRankBase):
    def _create_tables(self) -> None:
        num_features = 4
        self.tables += [
            EmbeddingBagConfig(
                num_embeddings=(i + 1) * 1000,
                embedding_dim=256,
                name="table_" + str(i),
                feature_names=["feature_" + str(i)],
                total_num_buckets=10,
                use_virtual_table=True,
            )
            for i in range(num_features)
        ]

    @staticmethod
    def _copy_ssd_emb_modules(
        m1: DistributedModelParallel, m2: DistributedModelParallel
    ) -> None:
        """
        Util function to copy and set the SSD TBE modules of two models. It
        requires both DMP modules to have the same sharding plan.
        """
        for lookup1, lookup2 in zip(
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ebc`.
            m1.module.sparse.ebc._lookups,
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ebc`.
            m2.module.sparse.ebc._lookups,
        ):
            for emb_module1, emb_module2 in zip(
                lookup1._emb_modules, lookup2._emb_modules
            ):
                ssd_emb_modules = {
                    ZeroCollisionKeyValueEmbeddingBag,
                    ZeroCollisionKeyValueEmbedding,
                }
                if type(emb_module1) in ssd_emb_modules:
                    assert type(emb_module1) is type(emb_module2), (
                        "Expect two emb_modules to be of the same type, either both "
                        "SSDEmbeddingBag or SSDEmbeddingBag."
                    )
                    emb_module1.flush()
                    emb_module2.flush()

                    emb1_kv = {
                        t: (sharded_t, sharded_w_id, bucket)
                        for t, sharded_t, sharded_w_id, bucket in emb_module1.get_named_split_embedding_weights_snapshot()
                    }
                    for (
                        t,
                        sharded_t2,
                        _,
                        _,
                    ) in emb_module2.get_named_split_embedding_weights_snapshot():
                        assert t in emb1_kv
                        sharded_t1 = emb1_kv[t][0]
                        sharded_w1_id = emb1_kv[t][1]
                        w1_id = sharded_w1_id.local_shards()[0].tensor

                        pmt1 = sharded_t1.local_shards()[0].tensor
                        w1 = pmt1.get_weights_by_ids(w1_id)

                        # write value into ssd for both emb module for later comparison
                        pmt2 = sharded_t2.local_shards()[0].tensor
                        pmt2.wrapped.set_weights_and_ids(w1, w1_id.view(-1))

                    # purge after loading. This is needed, since we pass a batch
                    # through dmp when instantiating them.
                    emb_module1.purge()
                    emb_module2.purge()

    @staticmethod
    def _copy_fused_modules_into_ssd_emb_modules(
        fused_m: DistributedModelParallel, ssd_m: DistributedModelParallel
    ) -> None:
        """
        Util function to copy from fused embedding module to SSD TBE for initialization. It
        requires both DMP modules to have the same sharding plan.
        """

        for fused_lookup, ssd_lookup in zip(
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ec`.
            fused_m.module.sparse.ebc._lookups,
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ec`.
            ssd_m.module.sparse.ebc._lookups,
        ):
            for fused_emb_module, ssd_emb_module in zip(
                fused_lookup._emb_modules, ssd_lookup._emb_modules
            ):
                ssd_emb_modules = {
                    ZeroCollisionKeyValueEmbeddingBag,
                    ZeroCollisionKeyValueEmbedding,
                }
                if type(ssd_emb_module) in ssd_emb_modules:
                    fused_state_dict = fused_emb_module.state_dict()
                    for (
                        t,
                        sharded_t,
                        _,
                        _,
                    ) in ssd_emb_module.get_named_split_embedding_weights_snapshot():
                        weight_key = f"{t}.weight"
                        fused_sharded_t = fused_state_dict[weight_key]
                        fused_weight = fused_sharded_t.local_shards()[0].tensor.to(
                            "cpu"
                        )

                        # write value into ssd for both emb module for later comparison
                        pmt = sharded_t.local_shards()[0].tensor
                        pmt.wrapped.set_range(0, 0, fused_weight.size(0), fused_weight)

                    # purge after loading. This is needed, since we pass a batch
                    # through dmp when instantiating them.
                    fused_emb_module.purge()
                    ssd_emb_module.purge()

    @unittest.skipIf(
        not torch.cuda.is_available(),
        "Not enough GPUs, this test requires at least one GPU",
    )
    # pyre-ignore[56]
    @given(
        sharder_type=st.sampled_from(
            [
                SharderType.EMBEDDING_BAG_COLLECTION.value,
            ]
        ),
        kernel_type=st.sampled_from(
            [
                EmbeddingComputeKernel.SSD_VIRTUAL_TABLE.value,
            ]
        ),
        sharding_type=st.sampled_from(
            [
                # TODO: add other test cases when kv embedding support other sharding
                ShardingType.ROW_WISE.value,
            ]
        ),
        is_training=st.booleans(),
        stochastic_rounding=st.booleans(),
        dtype=st.sampled_from([DataType.FP32, DataType.FP16]),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=4, deadline=None)
    def test_kv_zch_load_state_dict(
        self,
        sharder_type: str,
        kernel_type: str,
        sharding_type: str,
        is_training: bool,
        stochastic_rounding: bool,
        dtype: DataType,
    ) -> None:
        """
        This test checks that if SSD TBE is deterministic. That is, if two SSD
        TBEs start with the same state, they would produce the same output.
        """
        self._set_table_weights_precision(dtype)

        fused_params = {
            "learning_rate": 0.1,
            "stochastic_rounding": stochastic_rounding,
        }
        is_deterministic = dtype == DataType.FP32 or not stochastic_rounding
        constraints = {
            table.name: ParameterConstraints(
                sharding_types=[sharding_type],
                compute_kernels=[kernel_type],
            )
            for i, table in enumerate(self.tables)
        }
        sharders = [
            create_test_sharder(
                sharder_type,
                sharding_type,
                kernel_type,
                fused_params=fused_params,
            ),
        ]

        # pyre-ignore
        models, batch = self._generate_dmps_and_batch(sharders, constraints=constraints)
        m1, m2 = models

        # load state dict for dense modules
        m2.load_state_dict(cast("OrderedDict[str, torch.Tensor]", m1.state_dict()))
        self._copy_ssd_emb_modules(m1, m2)

        if is_training:
            self._train_models(m1, m2, batch)
        self._eval_models(m1, m2, batch, is_deterministic=is_deterministic)
        self._compare_models(
            m1, m2, is_deterministic=is_deterministic, use_virtual_table=True
        )

    @unittest.skipIf(
        not torch.cuda.is_available(),
        "Not enough GPUs, this test requires at least one GPU",
    )
    # pyre-ignore[56]
    @given(
        sharder_type=st.sampled_from(
            [
                SharderType.EMBEDDING_BAG_COLLECTION.value,
            ]
        ),
        kernel_type=st.sampled_from(
            [
                EmbeddingComputeKernel.SSD_VIRTUAL_TABLE.value,
            ]
        ),
        sharding_type=st.sampled_from(
            [
                # TODO: add other test cases when kv embedding support other sharding
                ShardingType.ROW_WISE.value,
            ]
        ),
        is_training=st.booleans(),
        stochastic_rounding=st.booleans(),
        dtype=st.sampled_from([DataType.FP32, DataType.FP16]),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=4, deadline=None)
    def test_kv_zch_numerical_accuracy(
        self,
        sharder_type: str,
        kernel_type: str,
        sharding_type: str,
        is_training: bool,
        stochastic_rounding: bool,
        dtype: DataType,
    ) -> None:
        """
        Make sure it produces same numbers as normal TBE.
        """
        self._set_table_weights_precision(dtype)

        base_kernel_type = EmbeddingComputeKernel.FUSED.value
        learning_rate = 0.1
        fused_params = {
            "optimizer": EmbOptimType.EXACT_ROWWISE_ADAGRAD,
            "learning_rate": learning_rate,
            "stochastic_rounding": stochastic_rounding,
        }
        is_deterministic = dtype == DataType.FP32 or not stochastic_rounding
        fused_sharders = [
            cast(
                ModuleSharder[nn.Module],
                create_test_sharder(
                    sharder_type,
                    sharding_type,
                    base_kernel_type,  # base kernel type
                    fused_params=fused_params,
                ),
            ),
        ]
        ssd_sharders = [
            cast(
                ModuleSharder[nn.Module],
                create_test_sharder(
                    sharder_type,
                    sharding_type,
                    kernel_type,
                    fused_params=fused_params,
                ),
            ),
        ]
        ssd_constraints = {
            table.name: ParameterConstraints(
                sharding_types=[sharding_type],
                compute_kernels=[kernel_type],
            )
            for i, table in enumerate(self.tables)
        }

        # for fused model, we need to change the table config to non-kvzch
        ssd_tables = copy.deepcopy(self.tables)
        for table in self.tables:
            table.total_num_buckets = None
            table.use_virtual_table = False
        (fused_model, _), _ = self._generate_dmps_and_batch(fused_sharders)
        self.tables = ssd_tables
        (ssd_model, _), batch = self._generate_dmps_and_batch(
            ssd_sharders, constraints=ssd_constraints
        )

        # load state dict for dense modules
        ssd_model.load_state_dict(
            cast("OrderedDict[str, torch.Tensor]", fused_model.state_dict())
        )

        # for this to work, we expect the order of lookups to be the same
        # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ebc`.
        assert len(fused_model.module.sparse.ebc._lookups) == len(
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ebc`.
            ssd_model.module.sparse.ebc._lookups
        ), "Expect same number of lookups"

        for fused_lookup, ssd_lookup in zip(
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ebc`.
            fused_model.module.sparse.ebc._lookups,
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ebc`.
            ssd_model.module.sparse.ebc._lookups,
        ):
            assert len(fused_lookup._emb_modules) == len(
                ssd_lookup._emb_modules
            ), "Expect same number of emb modules"

        self._copy_fused_modules_into_ssd_emb_modules(fused_model, ssd_model)

        if is_training:
            self._train_models(fused_model, ssd_model, batch)
        self._eval_models(
            fused_model, ssd_model, batch, is_deterministic=is_deterministic
        )

    @unittest.skipIf(
        not torch.cuda.is_available(),
        "Not enough GPUs, this test requires at least one GPU",
    )
    # pyre-ignore[56]
    @given(
        sharder_type=st.sampled_from(
            [
                SharderType.EMBEDDING_BAG_COLLECTION.value,
            ]
        ),
        kernel_type=st.sampled_from(
            [
                EmbeddingComputeKernel.SSD_VIRTUAL_TABLE.value,
            ]
        ),
        sharding_type=st.sampled_from(
            [
                # TODO: add other test cases when kv embedding support other sharding
                ShardingType.ROW_WISE.value,
            ]
        ),
        is_training=st.booleans(),
        stochastic_rounding=st.booleans(),
        dtype=st.sampled_from([DataType.FP32, DataType.FP16]),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=4, deadline=None)
    def test_kv_zch_fused_optimizer(
        self,
        sharder_type: str,
        kernel_type: str,
        sharding_type: str,
        is_training: bool,
        stochastic_rounding: bool,
        dtype: DataType,
    ) -> None:
        """
        Purpose of this test is to make sure it works with warm up policy.
        """
        self._set_table_weights_precision(dtype)

        is_deterministic = dtype == DataType.FP32 or not stochastic_rounding

        constraints = {
            table.name: ParameterConstraints(
                sharding_types=[sharding_type],
                compute_kernels=[kernel_type],
            )
            for i, table in enumerate(self.tables)
        }

        base_sharders = [
            create_test_sharder(
                sharder_type,
                sharding_type,
                kernel_type,
                fused_params={
                    "learning_rate": 0.2,
                    "stochastic_rounding": stochastic_rounding,
                },
            ),
        ]
        models, batch = self._generate_dmps_and_batch(
            base_sharders,  # pyre-ignore
            constraints=constraints,
        )
        base_model, _ = models

        test_sharders = [
            create_test_sharder(
                sharder_type,
                sharding_type,
                kernel_type,
                fused_params={
                    "learning_rate": 0.1,
                    "stochastic_rounding": stochastic_rounding,
                },
            ),
        ]
        models, _ = self._generate_dmps_and_batch(
            test_sharders,  # pyre-ignore
            constraints=constraints,
        )
        test_model, _ = models

        # load state dict for dense modules
        test_model.load_state_dict(
            cast("OrderedDict[str, torch.Tensor]", base_model.state_dict())
        )
        self._copy_ssd_emb_modules(base_model, test_model)

        self._eval_models(
            base_model, test_model, batch, is_deterministic=is_deterministic
        )

        # change learning rate for test_model
        fused_opt = test_model.fused_optimizer
        # pyre-ignore
        fused_opt.param_groups[0]["lr"] = 0.2
        fused_opt.zero_grad()

        if is_training:
            self._train_models(base_model, test_model, batch)
        self._eval_models(
            base_model, test_model, batch, is_deterministic=is_deterministic
        )
        self._compare_models(base_model, test_model, is_deterministic=is_deterministic)

    # TODO: uncomment this when we have multiple kernels in rw support(unblock input dist)
    # def test_ssd_mixed_kernels

    # TODO: uncomment this when we support different sharding types, e.g. tw, tw_rw together with rw
    # def test_ssd_mixed_sharding_types


class ZeroCollisionSequenceModelParallelStateDictTest(ModelParallelSingleRankBase):
    def setUp(self, backend: str = "nccl") -> None:
        self.shared_features = []
        self.embedding_groups = {}

        super().setUp(backend=backend)

    def _create_tables(self) -> None:
        num_features = 4
        shared_features = 2

        initial_tables = [
            EmbeddingConfig(
                num_embeddings=(i + 1) * 1000,
                embedding_dim=16,
                name="table_" + str(i),
                feature_names=["feature_" + str(i)],
                total_num_buckets=10,
                use_virtual_table=True,
            )
            for i in range(num_features)
        ]

        shared_features_tables = [
            EmbeddingConfig(
                num_embeddings=(i + 1) * 20,
                embedding_dim=16,
                name="table_" + str(i + num_features),
                feature_names=["feature_" + str(i)],
                total_num_buckets=10,
                use_virtual_table=True,
            )
            for i in range(shared_features)
        ]

        self.tables += initial_tables + shared_features_tables
        self.shared_features += [f"feature_{i}" for i in range(shared_features)]

        self.embedding_groups["group_0"] = [
            (f"{feature}@{table.name}" if feature in self.shared_features else feature)
            for table in self.tables
            for feature in table.feature_names
        ]

    def _create_model(self) -> nn.Module:
        return TestSequenceSparseNN(
            tables=self.tables,
            num_float_features=self.num_float_features,
            embedding_groups=self.embedding_groups,
            dense_device=self.device,
            sparse_device=torch.device("meta"),
        )

    @staticmethod
    def _copy_ssd_emb_modules(
        m1: DistributedModelParallel, m2: DistributedModelParallel
    ) -> None:
        """
        Util function to copy and set the SSD TBE modules of two models. It
        requires both DMP modules to have the same sharding plan.
        """
        for lookup1, lookup2 in zip(
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ec`.
            m1.module.sparse.ec._lookups,
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ec`.
            m2.module.sparse.ec._lookups,
        ):
            for emb_module1, emb_module2 in zip(
                lookup1._emb_modules, lookup2._emb_modules
            ):
                ssd_emb_modules = {
                    ZeroCollisionKeyValueEmbedding,
                }
                if type(emb_module1) in ssd_emb_modules:
                    assert type(emb_module1) is type(emb_module2), (
                        "Expect two emb_modules to be of the same type, either both "
                        "SSDEmbeddingBag or SSDEmbeddingBag."
                    )
                    emb_module1.flush()
                    emb_module2.flush()

                    emb1_kv = {
                        t: (sharded_t, sharded_w_id, bucket)
                        for t, sharded_t, sharded_w_id, bucket in emb_module1.get_named_split_embedding_weights_snapshot()
                    }
                    for (
                        t,
                        sharded_t2,
                        _,
                        _,
                    ) in emb_module2.get_named_split_embedding_weights_snapshot():
                        assert t in emb1_kv
                        sharded_t1 = emb1_kv[t][0]
                        sharded_w1_id = emb1_kv[t][1]
                        w1_id = sharded_w1_id.local_shards()[0].tensor

                        pmt1 = sharded_t1.local_shards()[0].tensor
                        w1 = pmt1.get_weights_by_ids(w1_id)

                        # write value into ssd for both emb module for later comparison
                        pmt2 = sharded_t2.local_shards()[0].tensor
                        pmt2.wrapped.set_weights_and_ids(w1, w1_id.view(-1))

                    # purge after loading. This is needed, since we pass a batch
                    # through dmp when instantiating them.
                    emb_module1.purge()
                    emb_module2.purge()

    @staticmethod
    def _copy_fused_modules_into_ssd_emb_modules(
        fused_m: DistributedModelParallel, ssd_m: DistributedModelParallel
    ) -> None:
        """
        Util function to copy from fused embedding module to SSD TBE for initialization. It
        requires both DMP modules to have the same sharding plan.
        """

        for fused_lookup, ssd_lookup in zip(
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ec`.
            fused_m.module.sparse.ec._lookups,
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ec`.
            ssd_m.module.sparse.ec._lookups,
        ):
            for fused_emb_module, ssd_emb_module in zip(
                fused_lookup._emb_modules, ssd_lookup._emb_modules
            ):
                ssd_emb_modules = {ZeroCollisionKeyValueEmbedding}
                if type(ssd_emb_module) in ssd_emb_modules:
                    fused_state_dict = fused_emb_module.state_dict()
                    for (
                        t,
                        sharded_t,
                        _,
                        _,
                    ) in ssd_emb_module.get_named_split_embedding_weights_snapshot():
                        weight_key = f"{t}.weight"
                        fused_sharded_t = fused_state_dict[weight_key]
                        fused_weight = fused_sharded_t.local_shards()[0].tensor.to(
                            "cpu"
                        )

                        # write value into ssd for both emb module for later comparison
                        pmt = sharded_t.local_shards()[0].tensor
                        pmt.wrapped.set_range(0, 0, fused_weight.size(0), fused_weight)

                    # purge after loading. This is needed, since we pass a batch
                    # through dmp when instantiating them.
                    fused_emb_module.purge()
                    ssd_emb_module.purge()

    @unittest.skipIf(
        not torch.cuda.is_available(),
        "Not enough GPUs, this test requires at least one GPU",
    )
    # pyre-ignore[56]
    @given(
        sharding_type=st.sampled_from(
            [
                # TODO: add other test cases when kv embedding support other sharding
                ShardingType.ROW_WISE.value,
            ]
        ),
        kernel_type=st.sampled_from(
            [
                EmbeddingComputeKernel.SSD_VIRTUAL_TABLE.value,
            ]
        ),
        is_training=st.booleans(),
        stochastic_rounding=st.booleans(),
        dtype=st.sampled_from([DataType.FP32, DataType.FP16]),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=4, deadline=None)
    def test_kv_zch_load_state_dict(
        self,
        sharding_type: str,
        kernel_type: str,
        is_training: bool,
        stochastic_rounding: bool,
        dtype: DataType,
    ) -> None:
        """
        This test checks that if SSD TBE is deterministic. That is, if two SSD
        TBEs start with the same state, they would produce the same output.
        """
        self._set_table_weights_precision(dtype)

        fused_params = {
            "learning_rate": 0.1,
            "stochastic_rounding": stochastic_rounding,
        }
        is_deterministic = dtype == DataType.FP32 or not stochastic_rounding
        sharders = [
            cast(
                ModuleSharder[nn.Module],
                TestEmbeddingCollectionSharder(
                    sharding_type=sharding_type,
                    kernel_type=kernel_type,
                    fused_params=fused_params,
                ),
            ),
        ]

        constraints = {
            table.name: ParameterConstraints(
                sharding_types=[sharding_type],
                compute_kernels=[kernel_type],
            )
            for i, table in enumerate(self.tables)
        }

        models, batch = self._generate_dmps_and_batch(sharders, constraints=constraints)
        m1, m2 = models

        # load state dict for dense modules
        m2.load_state_dict(cast("OrderedDict[str, torch.Tensor]", m1.state_dict()))
        self._copy_ssd_emb_modules(m1, m2)

        if is_training:
            self._train_models(m1, m2, batch)
        self._eval_models(m1, m2, batch, is_deterministic=is_deterministic)
        self._compare_models(
            m1, m2, is_deterministic=is_deterministic, use_virtual_table=True
        )

    @unittest.skipIf(
        not torch.cuda.is_available(),
        "Not enough GPUs, this test requires at least one GPU",
    )
    # pyre-ignore[56]
    @given(
        sharder_type=st.sampled_from(
            [
                SharderType.EMBEDDING_COLLECTION.value,
            ]
        ),
        kernel_type=st.sampled_from(
            [
                EmbeddingComputeKernel.SSD_VIRTUAL_TABLE.value,
            ]
        ),
        sharding_type=st.sampled_from(
            [
                # TODO: add other test cases when kv embedding support other sharding
                ShardingType.ROW_WISE.value,
            ]
        ),
        is_training=st.booleans(),
        stochastic_rounding=st.booleans(),
        dtype=st.sampled_from([DataType.FP32, DataType.FP16]),
    )
    @settings(verbosity=Verbosity.verbose, max_examples=4, deadline=None)
    def test_kv_zch_numerical_accuracy(
        self,
        sharder_type: str,
        kernel_type: str,
        sharding_type: str,
        is_training: bool,
        stochastic_rounding: bool,
        dtype: DataType,
    ) -> None:
        """
        Make sure it produces same numbers as normal TBE.
        """
        self._set_table_weights_precision(dtype)
        base_kernel_type = EmbeddingComputeKernel.FUSED.value
        learning_rate = 0.1
        fused_params = {
            "optimizer": EmbOptimType.EXACT_ROWWISE_ADAGRAD,
            "learning_rate": learning_rate,
            "stochastic_rounding": stochastic_rounding,
        }
        is_deterministic = dtype == DataType.FP32 or not stochastic_rounding
        fused_sharders = [
            cast(
                ModuleSharder[nn.Module],
                create_test_sharder(
                    sharder_type,
                    sharding_type,
                    base_kernel_type,  # base kernel type
                    fused_params=fused_params,
                ),
            ),
        ]
        ssd_sharders = [
            cast(
                ModuleSharder[nn.Module],
                create_test_sharder(
                    sharder_type,
                    sharding_type,
                    kernel_type,
                    fused_params=fused_params,
                ),
            ),
        ]
        ssd_constraints = {
            table.name: ParameterConstraints(
                sharding_types=[sharding_type],
                compute_kernels=[kernel_type],
            )
            for i, table in enumerate(self.tables)
        }

        # for fused model, we need to change the table config to non-kvzch
        ssd_tables = copy.deepcopy(self.tables)
        for table in self.tables:
            table.total_num_buckets = None
            table.use_virtual_table = False
        (fused_model, _), _ = self._generate_dmps_and_batch(fused_sharders)
        self.tables = ssd_tables
        (ssd_model, _), batch = self._generate_dmps_and_batch(
            ssd_sharders, constraints=ssd_constraints
        )

        # load state dict for dense modules
        ssd_model.load_state_dict(
            cast("OrderedDict[str, torch.Tensor]", fused_model.state_dict())
        )

        # for this to work, we expect the order of lookups to be the same
        # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ec`.
        assert len(fused_model.module.sparse.ec._lookups) == len(
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ec`.
            ssd_model.module.sparse.ec._lookups
        ), "Expect same number of lookups"

        for fused_lookup, ssd_lookup in zip(
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ec`.
            fused_model.module.sparse.ec._lookups,
            # pyre-fixme[16]: Item `Tensor` of `Tensor | Module` has no attribute `ec`.
            ssd_model.module.sparse.ec._lookups,
        ):
            assert len(fused_lookup._emb_modules) == len(
                ssd_lookup._emb_modules
            ), "Expect same number of emb modules"

        self._copy_fused_modules_into_ssd_emb_modules(fused_model, ssd_model)

        if is_training:
            self._train_models(fused_model, ssd_model, batch)
        self._eval_models(
            fused_model, ssd_model, batch, is_deterministic=is_deterministic
        )


# TODO: remove after development is done
def main() -> None:
    unittest.main()


if __name__ == "__main__":
    main()
