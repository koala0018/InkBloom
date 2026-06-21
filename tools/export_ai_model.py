"""Build-time only: export the BSD-licensed ECCV16 colorizer to ONNX."""
from __future__ import annotations

import sys
import types
import importlib
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".research-colorization-ai"
sys.path.insert(0, str(SOURCE))
sys.modules.setdefault("IPython", types.SimpleNamespace(embed=lambda *args, **kwargs: None))
package = types.ModuleType("colorizers")
package.__path__ = [str(SOURCE / "colorizers")]
sys.modules["colorizers"] = package
eccv16 = importlib.import_module("colorizers.eccv16").eccv16


def main() -> None:
    destination = ROOT / "assets" / "models" / "eccv16-colorizer.onnx"
    destination.parent.mkdir(parents=True, exist_ok=True)
    model = eccv16(pretrained=True).eval()
    sample = torch.zeros(1, 1, 256, 256, dtype=torch.float32)
    torch.onnx.export(
        model,
        sample,
        destination,
        input_names=["l_channel"],
        output_names=["ab_channels"],
        opset_version=17,
        do_constant_folding=True,
        dynamo=False,
    )
    print(destination, destination.stat().st_size)


if __name__ == "__main__":
    main()
