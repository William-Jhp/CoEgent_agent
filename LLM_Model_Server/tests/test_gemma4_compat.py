"""Check the exact KV-sharing boundary without importing a GPU runtime."""
from types import SimpleNamespace
import unittest
from local_llm_server.gemma4_compat import remove_unused_shared_key_norms, is_unused_key_norm


class Gemma4CompatTests(unittest.TestCase):
    def test_only_shared_key_norms_are_removed(self):
        attention=[SimpleNamespace(is_kv_shared_layer=shared,k_norm=object(),q_norm=object())
                   for shared in [False,False,True,True]]
        model=SimpleNamespace(model=SimpleNamespace(layers=[SimpleNamespace(self_attn=a) for a in attention]))
        shared=remove_unused_shared_key_norms(model)
        self.assertEqual(shared,{2,3})
        self.assertTrue(hasattr(attention[0],'k_norm'))
        self.assertTrue(hasattr(attention[1],'k_norm'))
        self.assertFalse(hasattr(attention[2],'k_norm'))
        self.assertFalse(hasattr(attention[3],'k_norm'))
        self.assertTrue(all(hasattr(a,'q_norm') for a in attention))

    def test_weight_filter_keeps_live_and_other_parameters(self):
        shared={15,16}
        self.assertTrue(is_unused_key_norm('model.language_model.layers.15.self_attn.k_norm.weight',shared))
        for name in ['model.language_model.layers.14.self_attn.k_norm.weight',
                     'model.language_model.layers.15.self_attn.q_norm.weight',
                     'model.language_model.layers.15.mlp.down_proj.weight']:
            self.assertFalse(is_unused_key_norm(name,shared))


if __name__=='__main__': unittest.main()
