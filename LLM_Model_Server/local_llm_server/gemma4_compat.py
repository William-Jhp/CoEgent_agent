"""Scoped compatibility for vLLM 0.19.1's unused Gemma4 KV-sharing key norms.

The upstream attention forward never calls k_norm in KV-shared layers. Recent
HF checkpoints omit those weights. Remove only those unused parameters before
the strict loader enumerates expected weights; preserve every live key norm.
"""
import importlib.metadata
import re


def remove_unused_shared_key_norms(model):
    shared = set()
    for index,layer in enumerate(model.model.layers):
        attention = layer.self_attn
        if attention.is_kv_shared_layer:
            if hasattr(attention,'k_norm'):
                del attention.k_norm
            shared.add(index)
    return shared


def is_unused_key_norm(name,shared):
    match = re.search(r'\.layers\.(\d+)\.self_attn\.k_norm\.',name)
    return match is not None and int(match.group(1)) in shared


def register():
    if importlib.metadata.version('vllm') != '0.19.1':
        raise RuntimeError('Gemma4 compatibility plugin requires vLLM 0.19.1; revalidate before upgrading')
    from vllm.model_executor.models import ModelRegistry
    ModelRegistry.register_model('Gemma4ForCausalLM',
        'local_llm_server.gemma4_compat_model:CompatibleGemma4ForCausalLM')
