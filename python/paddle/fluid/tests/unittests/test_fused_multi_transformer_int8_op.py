# Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
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

import unittest

import numpy as np
from test_sparse_attention_op import get_cuda_version

import paddle
import paddle.fluid.core as core
import paddle.nn.functional as F
from paddle import _legacy_C_ops, tensor
from paddle.fluid.framework import default_main_program
from paddle.nn.layer.common import Dropout
from paddle.nn.layer.norm import LayerNorm
from paddle.nn.layer.transformer import _convert_attention_mask

default_main_program().random_seed = 42
np.random.seed(0)


def fused_multi_transformer_int8(
    x,
    ln_scales,
    ln_biases,
    qkv_weights,
    qkv_biases,
    linear_weights,
    linear_biases,
    ffn_ln_scales,
    ffn_ln_biases,
    ffn1_weights,
    ffn1_biases,
    ffn2_weights,
    ffn2_biases,
    pre_layer_norm=True,
    epsilon=1e-05,
    cache_kvs=None,
    time_step=None,
    attn_mask=None,
    dropout_rate=0.0,
    activation="gelu",
    training=False,
    mode='upscale_in_train',
    trans_qkvw=True,
    ring_id=-1,
    name=None,
    qkv_out_scales=None,
    out_linear_out_scales=None,
    ffn1_out_scales=None,
    ffn2_out_scales=None,
    num_head=0,
    dim_head=0,
    dim_ffn=0,
    qkv_in_scale=[],
    out_linear_in_scale=[],
    ffn1_in_scale=[],
    ffn2_in_scale=[],
):
    mode = (
        'downgrade_in_infer' if mode == 'downscale_in_infer' else mode
    )  # semantic transfer

    cache_kv_out, final_out = _legacy_C_ops.fused_multi_transformer_int8(
        x,
        ln_scales,
        ln_biases,
        qkv_weights,
        qkv_biases,
        cache_kvs,
        time_step,
        attn_mask,
        linear_weights,
        linear_biases,
        ffn_ln_scales,
        ffn_ln_biases,
        ffn1_weights,
        ffn1_biases,
        ffn2_weights,
        ffn2_biases,
        qkv_out_scales,
        out_linear_out_scales,
        ffn1_out_scales,
        ffn2_out_scales,
        cache_kvs,
        'num_head',
        num_head,
        'dim_head',
        dim_head,
        'dim_ffn',
        dim_ffn,
        'qkv_in_scale',
        qkv_in_scale,
        'out_linear_in_scale',
        out_linear_in_scale,
        'ffn1_in_scale',
        ffn1_in_scale,
        'ffn2_in_scale',
        ffn2_in_scale,
        'pre_layer_norm',
        pre_layer_norm,
        'epsilon',
        epsilon,
        'dropout_rate',
        dropout_rate,
        'is_test',
        not training,
        'dropout_implementation',
        mode,
        'act_method',
        activation,
        'trans_qkvw',
        trans_qkvw,
        'ring_id',
        ring_id,
    )
    if cache_kvs is not None:
        return final_out, cache_kv_out
    return final_out


@unittest.skipIf(
    not core.is_compiled_with_cuda()
    or get_cuda_version() < 11020
    or paddle.device.cuda.get_device_capability()[0] < 8,
    "FusedMultiTransformerInt8 requires CUDA >= 11.2 and CUDA_ARCH >= 8",
)
class TestFusedMultiTransformerInt8Op(unittest.TestCase):
    def setUp(self):
        self.config()
        self.generate_input_data()

        self.rtol = 1e-5
        # FIXME(wangxi): Because there is a problem with the test precision
        #  on A100, atol is temporarily set to 1e-2, and it will be
        #  changed back after the precision problem is solved.
        self.atol = 1e-2
        # make sure local development precision
        if "V100" in paddle.device.cuda.get_device_name():
            self.atol = 1e-4
        if self.x_type is np.float16:
            self.atol = 1e-1

        paddle.set_default_dtype(self.x_type)
        self.__class__.op_type = "fused_multi_transformer_int8"
        # use autograd to check grad in this unittest.
        self.__class__.no_need_check_grad = True

        paddle.set_default_dtype(np.float32)
        self.norm = LayerNorm(
            self.embed_dim, weight_attr=False, bias_attr=False
        )
        self.ffn_norm = LayerNorm(
            self.embed_dim, weight_attr=False, bias_attr=False
        )

        paddle.set_default_dtype(self.x_type)
        self.dropout = Dropout(self.dropout_prob, mode="upscale_in_train")
        self.activation = getattr(F, self.act_method)

    def config(self):
        # for debug
        self.debug = False

        self.x_type = np.float32
        self.attn_mask_type = np.float64
        # self.attn_mask_type = np.bool
        self.pre_layer_norm = True
        self.has_attn_mask = True

        # has_cache_kv, gen_cache_kv, stage
        # False,        False,        not generation
        # True,         True,         generation context stage
        # True,         False,        generation decoder stage
        self.has_cache_kv = False
        self.gen_cache_kv = False

        self.training = False

        self.layers = 3
        self.batch_size = 1
        self.query_length = 1
        self.cache_length = 1
        self.head_dim = 64
        self.num_heads = 16
        self.embed_dim = self.head_dim * self.num_heads

        self.dropout_prob = 0.0
        self.attn_dropout_prob = 0.0
        self.act_method = 'gelu'
        self.weight_attr = None
        self.bias_attr = None
        self.kdim, self.vdim = self.embed_dim, self.embed_dim
        self.key_length, self.value_length = (
            self.query_length,
            self.query_length,
        )

    def generate_input_data(self):
        self.query = np.random.rand(
            self.batch_size, self.query_length, self.embed_dim
        ).astype(self.x_type)
        q_weight = np.random.randint(
            -64, 64, [self.embed_dim, self.embed_dim], np.int32
        ).astype('float64')
        k_weight = np.random.randint(
            -64, 64, [self.kdim, self.embed_dim], np.int32
        ).astype('float64')
        v_weight = np.random.randint(
            -64, 64, [self.vdim, self.embed_dim], np.int32
        ).astype('float64')

        self.q_weight_tensor = paddle.to_tensor(q_weight)
        self.k_weight_tensor = paddle.to_tensor(k_weight)
        self.v_weight_tensor = paddle.to_tensor(v_weight)

        out_weight = np.random.randint(
            -64, 64, [self.embed_dim, self.embed_dim], np.int32
        ).astype('float64')
        ffn1_weight = np.random.randint(
            -64, 64, [self.embed_dim, 4 * self.embed_dim], np.int32
        ).astype('float64')
        ffn2_weight = np.random.randint(
            -64, 64, [4 * self.embed_dim, self.embed_dim], np.int32
        ).astype('float64')

        self.out_weight_tensor = paddle.to_tensor(out_weight)
        self.ffn1_weight_tensor = paddle.to_tensor(ffn1_weight)
        self.ffn2_weight_tensor = paddle.to_tensor(ffn2_weight)

        q_proj_bias = np.random.rand(self.embed_dim).astype(self.x_type)
        k_proj_bias = np.random.rand(self.embed_dim).astype(self.x_type)
        v_proj_bias = np.random.rand(self.embed_dim).astype(self.x_type)

        self.q_proj_bias_tensor = paddle.to_tensor(q_proj_bias)
        self.k_proj_bias_tensor = paddle.to_tensor(k_proj_bias)
        self.v_proj_bias_tensor = paddle.to_tensor(v_proj_bias)

        out_linear_proj_bias = np.random.rand(self.embed_dim).astype(
            self.x_type
        )
        ffn1_proj_bias = np.random.rand(4 * self.embed_dim).astype(self.x_type)
        ffn2_proj_bias = np.random.rand(self.embed_dim).astype(self.x_type)

        self.out_linear_proj_bias_tensor = paddle.to_tensor(
            out_linear_proj_bias
        )
        self.ffn1_proj_bias_tensor = paddle.to_tensor(ffn1_proj_bias)
        self.ffn2_proj_bias_tensor = paddle.to_tensor(ffn2_proj_bias)

        out_seq_len = self.key_length

        self.qkv_in_scales = []
        self.qkv_out_scales = []
        self.out_linear_in_scales = []
        self.out_linear_out_scales = []
        self.ffn1_in_scales = []
        self.ffn1_out_scales = []
        self.ffn2_in_scales = []
        self.ffn2_out_scales = []

        if self.has_cache_kv:
            self.cache_kv = np.random.rand(
                2,
                self.batch_size,
                self.num_heads,
                self.cache_length,
                self.head_dim,
            ).astype(self.x_type)

            if self.gen_cache_kv:
                self.cache_kv[:] = 0
            else:
                out_seq_len += self.cache_length
        else:
            self.cache_kv = None

        if self.has_attn_mask:
            # [B, n_head, seq_len, out_seq_len]
            self.attn_mask = np.ones(
                (self.batch_size, 1, self.query_length, out_seq_len),
                dtype=self.attn_mask_type,
            )
            if self.attn_mask_type == np.int64:
                self.attn_mask = np.tril(self.attn_mask)
            elif self.attn_mask_type == np.float64:
                if self.has_cache_kv and not self.gen_cache_kv:
                    # NOTE: decoder stage, -1(out_seq_len) should no mask
                    self.attn_mask[:, :, :, -2] = 0.0
                    self.attn_mask = (self.attn_mask - 1.0) * 1e4
                else:
                    self.attn_mask = (np.tril(self.attn_mask) - 1.0) * 1e4
            elif self.attn_mask_type == np.bool_:
                if self.has_cache_kv and not self.gen_cache_kv:
                    self.attn_mask[:, :, :, -2] = 0
                else:
                    self.attn_mask = np.tril(self.attn_mask)
            else:
                raise ValueError(
                    "'attn_mask_type' should be 'int64' or 'float64'."
                )
        else:
            self.attn_mask = None

    def fake_quant(self, input, scale):
        quant_value = 127.0 * scale * paddle.cast(input, 'float32')
        quant_value = paddle.round(quant_value)

        # No need to clip here because scale is the max value

        return paddle.cast(quant_value, 'float64')

    def GetBaselineOut(self):
        paddle.disable_static(place=paddle.CUDAPlace(0))
        tensor_query = paddle.to_tensor(self.query, stop_gradient=False)

        cache_kvs = []
        cache_kv = None
        if self.has_cache_kv:
            cache_kv = paddle.to_tensor(self.cache_kv, stop_gradient=False)

        if self.has_attn_mask:
            attn_mask = paddle.to_tensor(self.attn_mask, stop_gradient=False)
        else:
            attn_mask = None
        for i in range(self.layers):
            residual = tensor_query
            ln1_out = tensor_query
            if self.pre_layer_norm:
                ln1_out = self.norm(tensor_query)
            max_v = paddle.max(paddle.abs(paddle.cast(ln1_out, 'float32')))[0]
            self.qkv_in_scales.append(1 / max_v)
            self.qkv_out_scales.append(max_v / (127.0 * 127.0))

            # quant ln1_out
            ln1_out = self.fake_quant(ln1_out, self.qkv_in_scales[i])

            q = paddle.nn.functional.linear(ln1_out, self.q_weight_tensor)
            # de quant
            q = paddle.cast(
                paddle.cast(q, 'float32') * self.qkv_out_scales[i],
                self.x_type,
            )

            q = q + self.q_proj_bias_tensor
            q = tensor.reshape(x=q, shape=[0, 0, self.num_heads, self.head_dim])
            q_out = tensor.transpose(x=q, perm=[0, 2, 1, 3])

            k = paddle.nn.functional.linear(ln1_out, self.k_weight_tensor)
            k = paddle.cast(
                paddle.cast(k, 'float32') * self.qkv_out_scales[i],
                self.x_type,
            )
            k = k + self.k_proj_bias_tensor
            v = paddle.nn.functional.linear(ln1_out, self.v_weight_tensor)
            v = paddle.cast(
                paddle.cast(v, 'float32') * self.qkv_out_scales[i],
                self.x_type,
            )
            v = v + self.v_proj_bias_tensor

            k = tensor.reshape(x=k, shape=[0, 0, self.num_heads, self.head_dim])
            k_out = tensor.transpose(x=k, perm=[0, 2, 1, 3])
            v = tensor.reshape(x=v, shape=[0, 0, self.num_heads, self.head_dim])
            v_out = tensor.transpose(x=v, perm=[0, 2, 1, 3])

            if self.has_cache_kv:
                # [1, B, n_head, cache_seq_len, head_dim]
                cache_k, cache_v = paddle.split(cache_kv, 2)
                cache_k = paddle.squeeze(cache_k, axis=0)
                cache_v = paddle.squeeze(cache_v, axis=0)
                # [B, n_head, cache_seq_len + seq_len, head_dim]
                # out_seq_len = cache_seq_len + seq_len
                if self.debug:
                    print('q out is')
                    print(q_out[0, 0, :, :])
                    print('cache k out seq=128')
                    print(k_out[0, 0, :, :])
                if self.gen_cache_kv:
                    cache_kvs.append((k_out, v_out))
                else:
                    k_out = paddle.concat([cache_k, k_out], axis=-2)
                    v_out = paddle.concat([cache_v, v_out], axis=-2)

            # [B, n_head, seq_len, head_dim] * [B, n_head, out_seq_len, head_dim]
            # --> [B, n_head, seq_len, out_seq_len]
            qk_out = paddle.matmul(x=q_out, y=k_out, transpose_y=True)
            qk_out = paddle.scale(qk_out, scale=self.head_dim**-0.5)

            if self.debug:
                print('qk out is')
                print(qk_out[0][0][0])

            if attn_mask is not None:
                attn_mask = _convert_attention_mask(attn_mask, qk_out.dtype)
                attn_mask_out = qk_out + attn_mask
                if self.debug:
                    print('attn mask out is')
                    print(attn_mask_out[0][0][0])
                softmax_out = F.softmax(attn_mask_out)
            else:
                softmax_out = F.softmax(qk_out)

            if self.debug:
                print('softmax out is')
                print(softmax_out[0][0][0])
            if self.dropout_prob:
                dropout_out = F.dropout(
                    softmax_out,
                    self.dropout_prob,
                    training=self.training,
                    mode="upscale_in_train",
                )
                # [B, n_head, seq_len, out_seq_len] * [B, n_head, out_seq_len, head_dim]
                # --> [B, n_head, seq_len, head_dim]
                qktv_out = tensor.matmul(dropout_out, v_out)
            else:
                qktv_out = tensor.matmul(softmax_out, v_out)

            fmha_out = tensor.transpose(qktv_out, perm=[0, 2, 1, 3])
            if self.debug:
                print('fmha out is')
                print(fmha_out[0][0][0])
            out_linear_in = tensor.reshape(
                x=fmha_out, shape=[0, 0, fmha_out.shape[2] * fmha_out.shape[3]]
            )

            max_v = paddle.max(
                paddle.abs(paddle.cast(out_linear_in, 'float32'))
            )[0]

            self.out_linear_in_scales.append(1 / max_v)
            self.out_linear_out_scales.append(max_v / (127.0 * 127.0))

            out_linear_in = self.fake_quant(
                out_linear_in, self.out_linear_in_scales[i]
            )

            out = paddle.nn.functional.linear(
                out_linear_in, self.out_weight_tensor
            )

            out = paddle.cast(
                paddle.cast(out, 'float32') * self.out_linear_out_scales[i],
                self.x_type,
            )

            out = out + self.out_linear_proj_bias_tensor

            residual_out = residual + self.dropout(out)
            if not self.pre_layer_norm:
                attn_out = self.norm(residual_out)
            else:
                attn_out = residual_out

            ffn_ln_out = attn_out
            if self.pre_layer_norm:
                ffn_ln_out = self.ffn_norm(attn_out)

            max_v = paddle.max(paddle.abs(paddle.cast(ffn_ln_out, 'float32')))[
                0
            ]
            self.ffn1_in_scales.append(1 / max_v)
            self.ffn1_out_scales.append(max_v / (127.0 * 127.0))
            ffn_ln_out = self.fake_quant(ffn_ln_out, self.ffn1_in_scales[i])

            ffn1_out = paddle.nn.functional.linear(
                ffn_ln_out, self.ffn1_weight_tensor
            )

            ffn1_out = paddle.cast(
                paddle.cast(ffn1_out, 'float32') * self.ffn1_out_scales[i],
                self.x_type,
            )

            ffn1_out = ffn1_out + self.ffn1_proj_bias_tensor
            ffn1_out = self.dropout(self.activation(ffn1_out))

            max_v = paddle.max(paddle.abs(paddle.cast(ffn1_out, 'float32')))[0]
            self.ffn2_in_scales.append(1 / max_v)
            self.ffn2_out_scales.append(max_v / (127.0 * 127.0))
            ffn1_out = self.fake_quant(ffn1_out, self.ffn2_in_scales[i])

            ffn2_out = paddle.nn.functional.linear(
                ffn1_out, self.ffn2_weight_tensor
            )

            ffn2_out = paddle.cast(
                paddle.cast(ffn2_out, 'float32') * self.ffn2_out_scales[i],
                self.x_type,
            )
            ffn2_out = ffn2_out + self.ffn2_proj_bias_tensor

            residual_out = attn_out + self.dropout(ffn2_out)
            final_out = residual_out
            if not self.pre_layer_norm:
                final_out = self.ffn_norm(residual_out)

            tensor_query = final_out

        if self.has_cache_kv and self.gen_cache_kv:
            return final_out, cache_kvs
        return final_out

    def GetFusedMultiTransformerOut(self):
        paddle.disable_static(place=paddle.CUDAPlace(0))

        ln_scale = paddle.ones([self.embed_dim], 'float32')
        ln_bias = paddle.zeros([self.embed_dim], 'float32')
        ffn_ln_scale = ln_scale
        ffn_ln_bias = ln_bias

        q_proj_weight = self.q_weight_tensor.numpy().transpose((1, 0))
        k_proj_weight = self.k_weight_tensor.numpy().transpose((1, 0))
        v_proj_weight = self.v_weight_tensor.numpy().transpose((1, 0))
        qkv_weight = np.concatenate(
            (q_proj_weight, k_proj_weight, v_proj_weight)
        )
        qkv_weight = qkv_weight.reshape(
            (3, self.num_heads, self.head_dim, self.embed_dim)
        )

        qkv_weight_tensor = paddle.to_tensor(qkv_weight)
        qkv_weight_tensor = paddle.cast(qkv_weight_tensor, 'int8')

        out_weight_tensor = paddle.cast(
            paddle.to_tensor(self.out_weight_tensor.numpy().transpose((1, 0))),
            'int8',
        )
        ffn1_weight_tensor = paddle.cast(
            paddle.to_tensor(self.ffn1_weight_tensor.numpy().transpose((1, 0))),
            'int8',
        )
        ffn2_weight_tensor = paddle.cast(
            paddle.to_tensor(self.ffn2_weight_tensor.numpy().transpose((1, 0))),
            'int8',
        )

        qkv_bias = np.concatenate(
            (
                self.q_proj_bias_tensor.numpy(),
                self.k_proj_bias_tensor.numpy(),
                self.v_proj_bias_tensor.numpy(),
            )
        )
        qkv_bias = qkv_bias.reshape((3, self.num_heads, self.head_dim))
        qkv_bias_tensor = paddle.to_tensor(qkv_bias)

        x = paddle.to_tensor(self.query, stop_gradient=True)
        cache_kvs, cache_kv = None, None
        time_step = None
        if self.has_cache_kv:
            cache_kvs = []

            max_seq_length = (self.cache_length + 128) // 128 * 128
            cache_kv = np.zeros(
                [
                    2,
                    self.batch_size,
                    self.num_heads,
                    max_seq_length,
                    self.head_dim,
                ],
                dtype=self.x_type,
            )

            elems = 4
            if self.x_type is np.float16:
                elems = 8

            assert self.head_dim % elems == 0
            v_elems = self.head_dim // elems

            # [B, num_head, 128, head_dim]
            # cache_k_tmp = self.cache_kv[0, :]
            # [B, num_head, 128, head_dim / 4, 4]
            cache_k_tmp = self.cache_kv[0].reshape(
                [
                    self.batch_size,
                    self.num_heads,
                    self.cache_length,
                    v_elems,
                    elems,
                ]
            )
            # [B, num_head, head_dim / 4, 128, 4]
            cache_k_tmp = cache_k_tmp.transpose([0, 1, 3, 2, 4])

            cache_kv[0, :].reshape(
                [
                    self.batch_size,
                    self.num_heads,
                    v_elems,
                    max_seq_length,
                    elems,
                ]
            )[:, :, :, : self.cache_length, :] = cache_k_tmp

            cache_kv[1, :, :, : self.cache_length, :] = self.cache_kv[1]
            if self.gen_cache_kv:
                assert self.query_length == self.cache_length
                cache_kv[:] = 0
            else:
                time_step = paddle.to_tensor(
                    [self.cache_length], dtype='int32', place=paddle.CPUPlace()
                )
        if self.has_attn_mask:
            attn_mask = paddle.to_tensor(self.attn_mask, stop_gradient=True)
        else:
            attn_mask = None
        epsilon = 1e-05
        ln2_epsilon = 1e-05

        if attn_mask is not None and self.attn_mask_type != np.bool_:
            attn_mask = _convert_attention_mask(attn_mask, x.dtype)

        qkv_weights, qkv_biases = [], []
        out_weights, out_biases = [], []
        ln_scales, ln_biases = [], []
        ffn1_weights, ffn1_biases = [], []
        ffn2_weights, ffn2_biases = [], []
        ffn_ln_scales, ffn_ln_biases = [], []

        # Input scales: list of value
        qkv_in_scale = []
        out_linear_in_scale = []
        ffn1_in_scale = []
        ffn2_in_scale = []

        # Output dequant scales: list of tensor
        qkv_out_scales = []
        out_linear_out_scales = []
        ffn1_out_scales = []
        ffn2_out_scales = []

        for i in range(self.layers):
            qkv_weights.append(qkv_weight_tensor)
            qkv_biases.append(qkv_bias_tensor)
            out_weights.append(out_weight_tensor)
            out_biases.append(self.out_linear_proj_bias_tensor)
            ln_scales.append(ln_scale)
            ln_biases.append(ln_bias)
            ffn1_weights.append(ffn1_weight_tensor)
            ffn1_biases.append(self.ffn1_proj_bias_tensor)
            ffn2_weights.append(ffn2_weight_tensor)
            ffn2_biases.append(self.ffn2_proj_bias_tensor)
            ffn_ln_scales.append(ffn_ln_scale)
            ffn_ln_biases.append(ffn_ln_bias)
            qkv_in_scale.append(self.qkv_in_scales[i])
            out_linear_in_scale.append(self.out_linear_in_scales[i])
            ffn1_in_scale.append(self.ffn1_in_scales[i])
            ffn2_in_scale.append(self.ffn2_in_scales[i])

            qkv_out_scale = (
                paddle.ones([3 * self.embed_dim], 'float32')
                * self.qkv_out_scales[i]
            )

            out_linear_out_scale = (
                paddle.ones([self.embed_dim], 'float32')
                * self.out_linear_out_scales[i]
            )

            ffn1_out_scale = (
                paddle.ones([4 * self.embed_dim], 'float32')
                * self.ffn1_out_scales[i]
            )

            ffn2_out_scale = (
                paddle.ones([self.embed_dim], 'float32')
                * self.ffn2_out_scales[i]
            )

            qkv_out_scales.append(qkv_out_scale)
            out_linear_out_scales.append(out_linear_out_scale)
            ffn1_out_scales.append(ffn1_out_scale)
            ffn2_out_scales.append(ffn2_out_scale)

            if self.has_cache_kv:
                cache_kvs.append(paddle.to_tensor(cache_kv, stop_gradient=True))

        final_out = fused_multi_transformer_int8(
            x,
            ln_scales,
            ln_biases,
            qkv_weights,
            qkv_biases,
            out_weights,
            out_biases,
            ffn_ln_scales,
            ffn_ln_biases,
            ffn1_weights,
            ffn1_biases,
            ffn2_weights,
            ffn2_biases,
            pre_layer_norm=self.pre_layer_norm,
            epsilon=epsilon,
            cache_kvs=cache_kvs,
            time_step=time_step,
            attn_mask=attn_mask,
            dropout_rate=self.dropout_prob,
            training=self.training,
            mode='upscale_in_train',
            trans_qkvw=True,
            ring_id=-1,
            name=None,
            qkv_out_scales=qkv_out_scales,
            out_linear_out_scales=out_linear_out_scales,
            ffn1_out_scales=ffn1_out_scales,
            ffn2_out_scales=ffn2_out_scales,
            num_head=self.num_heads,
            dim_head=self.head_dim,
            dim_ffn=4 * self.embed_dim,
            qkv_in_scale=qkv_in_scale,
            out_linear_in_scale=out_linear_in_scale,
            ffn1_in_scale=ffn1_in_scale,
            ffn2_in_scale=ffn2_in_scale,
        )

        if self.has_cache_kv:
            return final_out[0], final_out[1]

        return final_out

    def test_fused_multi_transformer_op(self):
        final_out_ref = self.GetBaselineOut()
        final_out = self.GetFusedMultiTransformerOut()
        if self.has_cache_kv:
            final_out, cache_kv_out = final_out
            s = cache_kv_out[0].shape
            bsz = s[1]
            num_head = s[2]
            max_seq_len = s[3]
            head_dim = s[4]
            elems = 8 if self.x_type is np.float16 else 4
            v_elems = head_dim // elems

            if self.debug:
                print("cache_k out timestep=128")
                print(
                    cache_kv_out[0].reshape(
                        [2, bsz, num_head, v_elems, max_seq_len, elems]
                    )[0, 0, 0, :, self.cache_length, :]
                )

                print("cache_v out timestep=128")
                print(cache_kv_out[0][1, 0, 0, self.cache_length, :])

            if self.gen_cache_kv:
                final_out_ref, cache_kvs = final_out_ref
                for i in range(self.layers):
                    cache_k_ref = cache_kvs[i][0]
                    cache_v_ref = cache_kvs[i][1]

                    cache_k = cache_kv_out[i][0, :]
                    cache_k = cache_k.reshape(
                        [bsz, num_head, v_elems, max_seq_len, elems]
                    )
                    cache_k = cache_k[:, :, :, : self.cache_length, :]
                    cache_k = cache_k.transpose([0, 1, 3, 2, 4])
                    cache_k = cache_k.reshape(
                        [bsz, num_head, self.cache_length, head_dim]
                    )

                    cache_v = cache_kv_out[i][1, :, :, : self.cache_length, :]

                    np.testing.assert_allclose(
                        cache_k_ref, cache_k, rtol=self.rtol, atol=self.atol
                    )
                    np.testing.assert_allclose(
                        cache_v_ref, cache_v, rtol=self.rtol, atol=self.atol
                    )
                    if i == 0:
                        break

        np.testing.assert_allclose(
            final_out_ref, final_out, rtol=self.rtol, atol=self.atol
        )


@unittest.skipIf(
    not core.is_compiled_with_cuda()
    or get_cuda_version() < 11020
    or paddle.device.cuda.get_device_capability()[0] < 8,
    "FusedMultiTransformerInt8 requires CUDA >= 11.2 and CUDA_ARCH >= 8",
)
class TestFusedMultiTransformerInt8OpFp16(TestFusedMultiTransformerInt8Op):
    def config(self):
        super().config()
        self.x_type = np.float16
        self.layers = 3  # odd layers


@unittest.skipIf(
    not core.is_compiled_with_cuda()
    or get_cuda_version() < 11020
    or paddle.device.cuda.get_device_capability()[0] < 8,
    "FusedMultiTransformerInt8 requires CUDA >= 11.2 and CUDA_ARCH >= 8",
)
class TestFusedMultiTransformerInt8OpCacheKV(TestFusedMultiTransformerInt8Op):
    def config(self):
        super().config()
        super().generate_input_data()
        self.has_cache_kv = True
        self.query_length = 1
        self.key_length, self.value_length = 1, 1
        self.layers = 3  # odd layers


@unittest.skipIf(
    not core.is_compiled_with_cuda()
    or get_cuda_version() < 11020
    or paddle.device.cuda.get_device_capability()[0] < 8,
    "FusedMultiTransformerInt8 requires CUDA >= 11.2 and CUDA_ARCH >= 8",
)
class TestFusedMultiTransformerInt8OpCacheKVFp16(
    TestFusedMultiTransformerInt8Op
):
    def config(self):
        super().config()
        self.has_cache_kv = True
        self.query_length = 1
        self.key_length, self.value_length = 1, 1
        self.x_type = np.float16


@unittest.skipIf(
    not core.is_compiled_with_cuda()
    or get_cuda_version() < 11020
    or paddle.device.cuda.get_device_capability()[0] < 8,
    "FusedMultiTransformerInt8 requires CUDA >= 11.2 and CUDA_ARCH >= 8",
)
class TestFusedMultiTransformerInt8OpGenCacheKV(
    TestFusedMultiTransformerInt8Op
):
    def config(self):
        super().config()
        self.has_cache_kv = True
        self.gen_cache_kv = True


@unittest.skipIf(
    not core.is_compiled_with_cuda()
    or get_cuda_version() < 11020
    or paddle.device.cuda.get_device_capability()[0] < 8,
    "FusedMultiTransformerInt8 requires CUDA >= 11.2 and CUDA_ARCH >= 8",
)
class TestFusedMultiTransformerInt8OpGenCacheKVFp16(
    TestFusedMultiTransformerInt8Op
):
    def config(self):
        super().config()
        self.has_cache_kv = True
        self.gen_cache_kv = True
        self.x_type = np.float16
        self.layers = 3  # odd layers


@unittest.skipIf(
    not core.is_compiled_with_cuda()
    or get_cuda_version() < 11020
    or paddle.device.cuda.get_device_capability()[0] < 8,
    "FusedMultiTransformerInt8 requires CUDA >= 11.2 and CUDA_ARCH >= 8",
)
class TestFusedMultiTransformerInt8OpPostLayerNormFp16(
    TestFusedMultiTransformerInt8Op
):
    def config(self):
        super().config()
        self.x_type = np.float16
        self.layers = 3  # odd layers
        self.pre_layer_norm = False


@unittest.skipIf(
    not core.is_compiled_with_cuda()
    or get_cuda_version() < 11020
    or paddle.device.cuda.get_device_capability()[0] < 8,
    "FusedMultiTransformerInt8 requires CUDA >= 11.2 and CUDA_ARCH >= 8",
)
class TestFusedMultiTransformerInt8OpCacheKVPostLayerNorm(
    TestFusedMultiTransformerInt8Op
):
    def config(self):
        super().config()
        self.has_cache_kv = True
        self.query_length = 1
        self.key_length, self.value_length = 1, 1
        self.layers = 3  # odd layers
        self.pre_layer_norm = False


@unittest.skipIf(
    not core.is_compiled_with_cuda()
    or get_cuda_version() < 11020
    or paddle.device.cuda.get_device_capability()[0] < 8,
    "FusedMultiTransformerInt8 requires CUDA >= 11.2 and CUDA_ARCH >= 8",
)
class TestFusedMultiTransformerInt8OpCacheKVPostLayerNormFp16(
    TestFusedMultiTransformerInt8Op
):
    def config(self):
        super().config()
        self.has_cache_kv = True
        self.query_length = 1
        self.key_length, self.value_length = 1, 1
        self.x_type = np.float16
        self.pre_layer_norm = False


@unittest.skipIf(
    not core.is_compiled_with_cuda()
    or get_cuda_version() < 11020
    or paddle.device.cuda.get_device_capability()[0] < 8,
    "FusedMultiTransformerInt8 requires CUDA >= 11.2 and CUDA_ARCH >= 8",
)
class TestFusedMultiTransformerInt8OpGenCacheKVPostLayerNorm(
    TestFusedMultiTransformerInt8Op
):
    def config(self):
        super().config()
        self.has_cache_kv = True
        self.gen_cache_kv = True
        self.pre_layer_norm = False


@unittest.skipIf(
    not core.is_compiled_with_cuda()
    or get_cuda_version() < 11020
    or paddle.device.cuda.get_device_capability()[0] < 8,
    "FusedMultiTransformerInt8 requires CUDA >= 11.2 and CUDA_ARCH >= 8",
)
class TestFusedMultiTransformerInt8OpGenCacheKVPostLayerNormFp16(
    TestFusedMultiTransformerInt8Op
):
    def config(self):
        super().config()
        self.has_cache_kv = True
        self.gen_cache_kv = True
        self.x_type = np.float16
        self.layers = 3  # odd layers
        self.pre_layer_norm = False


if __name__ == "__main__":
    unittest.main()
