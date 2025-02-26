# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import math
import os
import unittest

import paddle
import paddle.distributed.fleet.base.role_maker as role_maker
import paddle.fluid as fluid
from paddle.distributed.fleet import fleet

paddle.enable_static()


class TestDistFleetHeterProgram(unittest.TestCase):
    def build_role(self):
        environs = {}
        environs[
            "PADDLE_PSERVERS_IP_PORT_LIST"
        ] = "127.0.0.1:36012,127.0.0.1:36013"
        environs["PADDLE_TRAINER_ENDPOINTS"] = "127.0.0.1:36014,127.0.0.1:36015"
        environs[
            "PADDLE_ALL_HETER_TRAINER_IP_PORT_LIST"
        ] = "127.0.0.1:36016,127.0.0.1:36017"
        environs[
            "PADDLE_PREVIOUS_HETER_TRAINER_IP_PORT_LIST"
        ] = "127.0.0.1:36014,127.0.0.1:36015"
        environs["PADDLE_HETER_TRAINER_DEVICE"] = "gpu"
        environs["TRAINING_ROLE"] = "HETER_TRAINER"
        environs["STAGE_ID"] = 2
        environs["STAGE_NUM"] = 2
        environs["HETER_DEVICE_TYPE"] = "gpu"
        environs["PADDLE_STAGE_TRAINERS_NUM"] = [2, 2]
        environs["PADDLE_TRAINERS_NUM"] = 2
        environs["PADDLE_TRAINER_ID"] = 0
        environs["POD_IP"] = "127.0.0.1"
        environs["PADDLE_PORT"] = "36016"
        environs["FLAGS_selected_gpus"] = 0

        for k, v in environs.items():
            os.environ[k] = str(v)

        self.role = role_maker.PaddleCloudRoleMaker()
        return self.role

    def build_strategy(self):
        self.strategy = paddle.distributed.fleet.DistributedStrategy()
        self.strategy.a_sync = True
        self.strategy.a_sync_configs = {
            "launch_barrier": False,
            "heter_worker_device_guard": "gpu",
        }
        return self.strategy

    def build_input(self):
        dense_input = fluid.layers.data(
            name="dense_input", shape=[10], dtype="float32"
        )

        sparse_input_ids = [
            fluid.layers.data(
                name="C" + str(i), shape=[1], lod_level=1, dtype="int64"
            )
            for i in range(1, 27)
        ]

        label = fluid.layers.data(name="label", shape=[1], dtype="float32")

        inputs = [dense_input] + sparse_input_ids + [label]
        return inputs

    def build_net(self, inputs):
        def embedding_layer(input):
            return fluid.layers.embedding(
                input=input,
                is_sparse=True,
                size=[100001, 10],
                param_attr=fluid.ParamAttr(
                    name="SparseFeatFactors",
                    initializer=fluid.initializer.Uniform(),
                ),
            )

        sparse_embed_seq = list(map(embedding_layer, inputs[1:-1]))

        concated = fluid.layers.concat(sparse_embed_seq + inputs[0:1], axis=1)

        with fluid.device_guard("gpu"):
            fc1 = paddle.static.nn.fc(
                x=concated,
                size=400,
                activation="relu",
                weight_attr=fluid.ParamAttr(
                    initializer=fluid.initializer.Normal(
                        scale=1 / math.sqrt(concated.shape[1])
                    )
                ),
                name="fc1",
            )

        with fluid.device_guard("cpu"):
            fc2 = paddle.static.nn.fc(
                x=fc1,
                size=400,
                activation="relu",
                weight_attr=fluid.ParamAttr(
                    initializer=fluid.initializer.Normal(
                        scale=1 / math.sqrt(fc1.shape[1])
                    )
                ),
                name="fc2",
            )

        with fluid.device_guard("gpu"):
            fc3 = paddle.static.nn.fc(
                x=fc2,
                size=400,
                activation="relu",
                weight_attr=fluid.ParamAttr(
                    initializer=fluid.initializer.Normal(
                        scale=1 / math.sqrt(fc2.shape[1])
                    )
                ),
                name="fc3",
            )

        with fluid.device_guard("cpu"):
            predict = paddle.static.nn.fc(
                x=fc3,
                size=2,
                activation="softmax",
                weight_attr=fluid.ParamAttr(
                    initializer=fluid.initializer.Normal(
                        scale=1 / math.sqrt(fc3.shape[1])
                    )
                ),
            )

        with fluid.device_guard("gpu"):
            labels = fluid.layers.cast(inputs[-1], dtype="int64")
            cost = paddle.nn.functional.cross_entropy(
                input=predict, label=labels, reduction='none', use_softmax=False
            )
            avg_cost = paddle.sum(cost)

        return avg_cost

    def build_optimizer(self, avg_cost, strategy):
        optimizer = fluid.optimizer.SGD(1e-2)
        optimizer = fleet.distributed_optimizer(optimizer, strategy=strategy)
        optimizer.minimize(avg_cost)

    def test(self):
        role = self.build_role()
        fleet.init(role)
        strategy = self.build_strategy()
        inputs = self.build_input()
        avg_cost = self.build_net(inputs)
        self.build_optimizer(avg_cost, strategy)


if __name__ == "__main__":
    unittest.main()
