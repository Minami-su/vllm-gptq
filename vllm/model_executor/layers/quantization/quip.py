from typing import Any, Dict, List, Optional

import torch
from torch.nn.parameter import Parameter

from vllm._C import ops
from vllm.model_executor.layers.linear import (LinearMethodBase,
                                               set_weight_attrs)
from vllm.model_executor.layers.quantization.base_config import (
    QuantizationConfig)
from vllm.model_executor.layers.quantization.quip_utils import (
    get_packed_abs_grid,
    get_hadK,
    matmul_hadUt_cuda,
    matmul_hadU_cuda,
)


class QuipConfig(QuantizationConfig):
    """Config class for Quip.

    Reference: https://cornell-relaxml.github.io/quip-sharp/
    """

    def __init__(
        self,
        codebook: int,
    ) -> None:
        self.codebook = codebook

        if self.codebook != "E8P12":
            raise ValueError(
                "Currently, only E8P12 is supported for "
                f"Quip, but got {self.codebook}.")

    def __repr__(self) -> str:
        return (f"QuipConfig(codebook={self.codebook}, "
                f"rescale_WH={self.rescale_WH})")

    @classmethod
    def get_name(cls) -> str:
        return "quip"

    @classmethod
    def get_supported_act_dtypes(cls) -> List[torch.dtype]:
        return [torch.half]

    @classmethod
    def get_min_capability(cls) -> int:
        return 80

    @classmethod
    def get_config_filenames(cls) -> List[str]:
        return ["quantization_config.json"]

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "QuipConfig":
        codebook = cls.get_from_keys(config, ["codebook"])
        return cls(codebook)

    def get_linear_method(self) -> "QuipLinearMethod":
        return QuipLinearMethod(self)

    def get_scaled_act_names(self) -> List[str]:
        return []

    def merge_weight(self) -> bool:
        return False


class QuipLinearMethod(LinearMethodBase):
    """Linear method for Quip.

    Args:
        quant_config: The Quip quantization config.
    """

    def __init__(self, quant_config: QuipConfig):
        self.quant_config = quant_config
        self.grid_packed_abs = get_packed_abs_grid().to(device="cuda")
        self.pack = 8
        self.idx_dtype = torch.int16

    def create_weights(
        self,
        input_size_per_partition: int,
        output_size_per_partition: int,
        input_size: int,
        output_size: int,
        params_dtype: torch.dtype,
    ) -> Dict[str, Any]:
        if input_size != input_size_per_partition or output_size != output_size_per_partition:
            raise ValueError(
                "Currently Quip doesn't support tensor parallel yet")

        had_left, K_left, q_in_features = get_hadK(input_size)
        had_right, K_right, q_out_features = get_hadK(output_size)
        weights = {
            "K_left": K_left,
            "K_right": K_right,
            "q_in_features": q_in_features,
            "q_out_features": q_out_features,
        }
        if had_left is not None:
            weights["had_left"] = had_left.to(dtype=params_dtype, device="cuda")
        if had_right is not None:
            weights["had_right"] = had_right.to(dtype=params_dtype, device="cuda")

        Qidxs = Parameter(
            torch.empty(q_out_features,
                        q_in_features // self.pack,
                        device="cuda",
                        dtype=self.idx_dtype
                        ),
            requires_grad=False,
        )
        set_weight_attrs(Qidxs, {"ignore_warning": True})
        SU = Parameter(
            torch.empty(input_size,
                        device="cuda",
                        dtype=params_dtype,
                        ),
            requires_grad=False,
        )
        set_weight_attrs(SU, {"ignore_warning": True})
        SV = Parameter(
            torch.empty(output_size,
                        device="cuda",
                        dtype=params_dtype,
                        ),
            requires_grad=False,
        )
        set_weight_attrs(SV, {"ignore_warning": True})
        weights.update({
            "Qidxs": Qidxs,
            "SU": SU,
            "SV": SV,
        })
        return weights

    def apply_weights(self,
                      weights: Dict[str, Any],
                      x: torch.Tensor,
                      bias: Optional[torch.Tensor] = None) -> torch.Tensor:
        reshaped_x = x.reshape(-1, x.shape[-1])
        out_dim = weights["SV"].shape[0]

        reshaped_x = reshaped_x * weights["SU"]
        reshaped_x = matmul_hadUt_cuda(reshaped_x, weights.get("had_left", None),
                                       weights["K_left"],
                                       weights["q_in_features"])

        m, n = weights["Qidxs"].shape
        if reshaped_x.size(0) < 32:
            out = ops.quip_gemv(reshaped_x,
                                weights["Qidxs"],
                                self.grid_packed_abs)
        else:
            W_decompressed = torch.empty(
                m, n * 8, dtype=torch.float16, device=x.device
            )
            ops.quip_decompress(
                weights["Qidxs"], self.grid_packed_abs, W_decompressed
            )
            out = reshaped_x @ W_decompressed.T

        out = matmul_hadU_cuda(out, weights.get("had_right", None), weights["K_right"],
                               weights["q_out_features"])[..., :out_dim]
        out = out * weights["SV"]
        out = out.view(*x.shape[:-1], out.shape[-1])
        out = out + bias if bias is not None else out
        return out