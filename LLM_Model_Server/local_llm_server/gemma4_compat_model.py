"""Loaded lazily by vLLM only; management commands never import this module."""
from vllm.model_executor.models.gemma4 import Gemma4ForCausalLM
from .gemma4_compat import remove_unused_shared_key_norms, is_unused_key_norm


class CompatibleGemma4ForCausalLM(Gemma4ForCausalLM):
    def __init__(self, *, vllm_config, prefix=''):
        super().__init__(vllm_config=vllm_config,prefix=prefix)
        self._unused_key_norm_layers = remove_unused_shared_key_norms(self)

    def load_weights(self,weights):
        # Older checkpoints may still contain these unused shared-layer norms.
        live_weights = ((name,weight) for name,weight in weights
                        if not is_unused_key_norm(name,self._unused_key_norm_layers))
        return super().load_weights(live_weights)
