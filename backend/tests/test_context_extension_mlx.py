"""Optional numerical checks: OLCR_TEST_MLX_CONTEXT=1, using .venv-mlx Python."""
import os
import unittest


@unittest.skipUnless(os.environ.get('OLCR_TEST_MLX_CONTEXT') == '1', 'requires MLX and Metal')
class MlxContextExtensionTests(unittest.TestCase):
    def test_yarn_text_and_multimodal_frequencies_scale_consistently(self):
        import mlx.core as mx
        import mlx.nn as nn
        from mlx_lm.models.rope_utils import YarnRoPE
        from mlx_vlm.models.qwen3_5.language import Qwen3_5RotaryEmbedding
        from olcr_api.context_extension import apply_yarn
        class Attention(nn.Module):
            def __init__(self, multimodal):
                super().__init__()
                if multimodal:
                    self.rotary_emb = Qwen3_5RotaryEmbedding(64, 4096, 10000, [11, 11, 10])
                else:
                    self.rope = nn.RoPE(64, traditional=False, base=10000)
        config = {'max_position_embeddings':4096, 'hidden_size':256, 'num_attention_heads':4, 'rope_theta':10000}
        expected = YarnRoPE(64, base=10000, scaling_factor=4, original_max_position_embeddings=4096)
        for multimodal in (False, True):
            model = Attention(multimodal)
            original = getattr(model, 'rotary_emb' if multimodal else 'rope')
            self.assertEqual('native_window', apply_yarn(model, config, 4096)['status'])
            self.assertIs(original, getattr(model, 'rotary_emb' if multimodal else 'rope'))
            self.assertEqual(4, apply_yarn(model, config, 16384)['factor'])
            if multimodal:
                self.assertTrue(mx.allclose(model.rotary_emb.inv_freq, 1 / expected._freqs).item())
                self.assertAlmostEqual(expected.mscale, model.rotary_emb.attention_scaling)
                self.assertFalse(model.rotary_emb.fused_apply)
                q = mx.ones((1, 2, 4, 64))
                k = mx.ones((1, 1, 4, 64))
                pos = mx.broadcast_to(mx.arange(8192, 8196)[None,None,:], (3,1,4))
                output = model.rotary_emb.apply_rotary(q, k, pos)
                mx.eval(output)
                self.assertTrue(all(mx.all(mx.isfinite(x)).item() for x in output))
            else:
                output = model.rope(mx.ones((1, 1, 4, 64)), offset=8192)
                self.assertTrue(mx.all(mx.isfinite(output)).item())
