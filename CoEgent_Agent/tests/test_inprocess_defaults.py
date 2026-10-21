"""Backend defaults must not redirect HTTP or explicit model selections."""
from argparse import Namespace
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from coegent import cli
from coegent.planner import TransformersModelPlanner


def args(**overrides):
    values = dict(model=None, endpoint=None, adapter=None, max_tokens=32,
                  selector_model=None, selector_endpoint=None, selector_adapter=None,
                  selector_max_tokens=16, planner_timeout=5, generation_mode='auto')
    values.update(overrides)
    return Namespace(**values)


@pytest.fixture
def loading(monkeypatch):
    seen = {}
    def tokenizer(model, **kwargs):
        seen['model'] = model
        return object()
    def weights(model, **kwargs):
        return SimpleNamespace(eval=lambda: None)
    def adapter(model, path, **kwargs):
        seen['adapter'] = path
        return model
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(bfloat16='bf16'))
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(
        AutoTokenizer=SimpleNamespace(from_pretrained=tokenizer),
        AutoModelForCausalLM=SimpleNamespace(from_pretrained=weights)))
    monkeypatch.setitem(sys.modules, 'peft', SimpleNamespace(
        PeftModel=SimpleNamespace(from_pretrained=adapter)))
    return seen


@pytest.mark.parametrize('selector', [False, True])
def test_hf_without_model_resolves_local_e4b_from_any_working_directory(monkeypatch, tmp_path, selector, loading):
    # Intercept only heavyweight loading; exercise real constructor/default resolution.
    monkeypatch.chdir(tmp_path)
    planner = cli.model_planner('hf', args(), selector=selector)
    expected = Path(__file__).resolve().parents[2] / 'LLM_Model_Server/model/hf/gemma-4-E4B-it'
    assert Path(loading['model']) == expected
    assert Path(planner.model_path) == expected
    assert 'adapter' not in loading
    assert planner.max_tokens == (16 if selector else 32)


def test_explicit_inprocess_checkpoint_and_adapter_remain_selectable(loading):
    cli.model_planner('hf', args(model='/custom/model', adapter='/custom/adapter'))
    assert loading == {'model': '/custom/model', 'adapter': '/custom/adapter'}


def test_http_without_model_still_requires_server_model_name():
    with pytest.raises(ValueError, match='model name/path is required'):
        cli.model_planner('http', args(endpoint='http://localhost:18520'))
